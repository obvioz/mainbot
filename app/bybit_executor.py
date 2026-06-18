from __future__ import annotations

import ccxt

from app.settings import settings
from app.bybit_sync import sync_bybit_time, private_call_with_time_retry


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
    try:
        ex = make_trade_exchange()
        symbol = f"{coin.upper()}/{settings.quote}"
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
