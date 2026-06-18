from __future__ import annotations

import math

import ccxt

from app.settings import settings
from app.bybit_sync import sync_bybit_time, private_call_with_time_retry
from app.error_log import log_error

# Стоимость выше Nx лимита авто-ордера считаем не «слегка превышено», а явно
# битым состоянием (огромный/мусорный qty) — отклоняем с отдельной формулировкой.
ABSURD_ORDER_VALUE_MULTIPLIER = 10


def _finite_positive(x) -> bool:
    """True только для конечного положительного числа (отсекает 0, <0, NaN, inf, мусор)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def _reject_auto_order(side: str, coin: str, qty, value_usdt, reason: str, extra: dict | None = None) -> dict:
    """Залогировать отклонение авто-ордера в error_log и вернуть ok=False.

    Состояние позиции не трогается — вызывающий код (monitor) при ok=False не
    выполняет никаких мутаций (см. C1+C2) и шлёт уведомление в Telegram.
    """
    try:
        log_error(
            "bybit_executor.guard",
            RuntimeError(reason),
            context={
                "side": side,
                "coin": coin,
                "qty": repr(qty),
                "estimated_value_usdt": value_usdt,
                "max_auto_order_usdt": float(settings.max_auto_order_usdt),
            },
        )
    except Exception:
        pass
    result = {"ok": False, "error": reason, "price": 0.0, "qty_executed": 0.0}
    if extra:
        result.update(extra)
    return result


def _check_auto_order_value(side: str, coin: str, value_usdt: float) -> dict | None:
    """Проверка потолка авто-ордера. None = можно исполнять, dict = отклонение."""
    cap = float(settings.max_auto_order_usdt)
    if value_usdt > ABSURD_ORDER_VALUE_MULTIPLIER * cap:
        return _reject_auto_order(
            side, coin, None, round(value_usdt, 2),
            f"Явно битый ордер: стоимость ${value_usdt:.2f} >> лимита "
            f"${cap:.2f} (x{ABSURD_ORDER_VALUE_MULTIPLIER}) — состояние повреждено",
        )
    if value_usdt > cap:
        return _reject_auto_order(
            side, coin, None, round(value_usdt, 2),
            f"Ордер ${value_usdt:.2f} превышает лимит авто-ордера ${cap:.2f}",
        )
    return None


def make_trade_exchange():
    key = settings.bybit_trade_api_key or settings.bybit_api_key
    secret = settings.bybit_trade_api_secret or settings.bybit_api_secret
    if not key or not secret:
        raise RuntimeError("Нет API ключей (BYBIT_TRADE_API_KEY/SECRET)")
    exchange_cls = getattr(ccxt, settings.exchange_id)
    ex = exchange_cls({
        "apiKey": key,
        "secret": secret,
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "spot",
            "accountType": "UNIFIED",
            "adjustForTimeDifference": True,
            "recvWindow": int(settings.bybit_recv_window),
        },
    })
    sync_bybit_time(ex)
    return ex


def execute_market_buy(coin: str, usdt_amount: float, reason: str = "") -> dict:
    """Place a market buy order on Bybit spot for a given USDT amount.

    Fetches the current ask price to compute qty, then places the order.
    Returns {"ok": bool, "order_id", "price", "qty_executed", "usdt_spent", "error"}.
    """
    if not settings.tp_auto_execute:
        return {"ok": False, "error": "TP_AUTO_EXECUTE отключён", "price": 0.0, "qty_executed": 0.0, "usdt_spent": 0.0}

    # Санити: сумма ордера должна быть конечным положительным числом.
    if not _finite_positive(usdt_amount):
        return _reject_auto_order("buy", coin, usdt_amount, None,
                                  f"Некорректная сумма ордера: {usdt_amount!r}", {"usdt_spent": 0.0})
    # Потолок авто-ордера: для покупки оценка стоимости = запрашиваемая сумма USDT.
    capped = _check_auto_order_value("buy", coin, float(usdt_amount))
    if capped is not None:
        capped.setdefault("usdt_spent", 0.0)
        return capped

    try:
        ex = make_trade_exchange()
        symbol = f"{coin.upper()}/{settings.quote}"
        ticker = ex.fetch_ticker(symbol)
        ask = float(ticker.get("ask") or ticker.get("last") or 0)
        if ask <= 0:
            return {"ok": False, "error": "Не удалось получить цену", "price": 0.0, "qty_executed": 0.0, "usdt_spent": 0.0}
        market = ex.market(symbol)
        min_amount = float((market.get("limits") or {}).get("amount", {}).get("min") or 0)
        qty = usdt_amount / ask
        if not _finite_positive(qty):
            return _reject_auto_order("buy", coin, qty, None,
                                      f"Некорректный qty: {qty!r}", {"usdt_spent": 0.0})
        if min_amount and qty < min_amount:
            return {"ok": False, "error": f"qty {qty:.6f} ниже минимального {min_amount}", "price": ask, "qty_executed": 0.0, "usdt_spent": 0.0}
        qty = float(ex.amount_to_precision(symbol, qty))
        order = private_call_with_time_retry(
            ex,
            ex.create_order,
            symbol,
            "market",
            "buy",
            qty,
        )
        price = float(order.get("average") or order.get("price") or ask)
        qty_filled = float(order.get("filled") or qty)
        return {
            "ok": True,
            "order_id": order.get("id"),
            "price": price,
            "qty_executed": qty_filled,
            "usdt_spent": round(price * qty_filled, 4),
            "error": None,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "price": 0.0, "qty_executed": 0.0, "usdt_spent": 0.0}


def execute_market_sell(coin: str, qty: float, reason: str = "") -> dict:
    """Place a market sell order on Bybit spot.

    Returns {"ok": bool, "order_id", "price", "qty_executed", "error"}.
    Returns ok=False without raising if auto-execute is disabled or keys are missing.
    """
    if not settings.tp_auto_execute:
        return {"ok": False, "error": "TP_AUTO_EXECUTE отключён", "price": 0.0, "qty_executed": qty}

    # Санити: qty должен быть конечным положительным числом (защита от битого
    # состояния — отрицательное/ноль/NaN/inf/мусор).
    if not _finite_positive(qty):
        return _reject_auto_order("sell", coin, qty, None, f"Некорректный qty: {qty!r}")

    try:
        ex = make_trade_exchange()
        symbol = f"{coin.upper()}/{settings.quote}"
        # Оцениваем стоимость продажи по текущей цене для проверки потолка.
        ticker = ex.fetch_ticker(symbol)
        price_est = float(ticker.get("last") or ticker.get("bid") or ticker.get("close") or 0)
        if price_est <= 0 or not math.isfinite(price_est):
            return _reject_auto_order("sell", coin, qty, None,
                                      "Не удалось оценить стоимость ордера (нет цены)")
        capped = _check_auto_order_value("sell", coin, float(qty) * price_est)
        if capped is not None:
            return capped

        order = private_call_with_time_retry(
            ex,
            ex.create_order,
            symbol,
            "market",
            "sell",
            qty,
        )
        price = float(order.get("average") or order.get("price") or 0)
        qty_filled = float(order.get("filled") or qty)
        return {
            "ok": True,
            "order_id": order.get("id"),
            "price": price,
            "qty_executed": qty_filled,
            "error": None,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "price": 0.0, "qty_executed": 0.0}
