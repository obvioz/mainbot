from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import ccxt

from app.settings import settings
from app.strategy_params import get_tp_levels
from app.config import COINS, TAKE_PROFIT_LEVELS, CATEGORIES, normalize_coin
from app.market import make_exchange, analyze_coin
from app.formatters import fmt_money, fmt_usdt
from app.storage import get_open_positions, save_portfolio, load_portfolio, add_journal, now_iso


@dataclass
class BybitAsset:
    coin: str
    qty: float
    free: float
    used: float
    avg_price: float
    invested_usdt: float
    current_price: float
    current_value: float
    pnl_usdt: float
    pnl_pct: float
    source: str


def _is_bybit_time_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("retcode" in msg and "10002" in msg) or "recv_window" in msg or "server timestamp" in msg


def sync_bybit_time(exchange, safety_ms: int | None = None):
    """Force Bybit timestamp slightly behind exchange server time.

    Bybit rejects private requests when the local timestamp is even a little
    ahead of server time. `adjustForTimeDifference` is not always enough on
    Windows, so we override ccxt nonce after reading Bybit public server time.
    """
    safety = int(safety_ms if safety_ms is not None else settings.bybit_time_safety_ms)
    try:
        server_ms = int(exchange.fetch_time())
        local_ms = int(time.time() * 1000)
        offset_ms = server_ms - local_ms - safety
        exchange.options["timeDifference"] = local_ms - server_ms
        exchange.options["recvWindow"] = int(settings.bybit_recv_window)
        exchange.nonce = lambda: int(time.time() * 1000) + offset_ms
        return {"server_ms": server_ms, "local_ms": local_ms, "offset_ms": offset_ms, "safety_ms": safety}
    except Exception:
        # If public time endpoint is unavailable, keep normal ccxt behavior.
        return None


def make_private_exchange():
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        raise RuntimeError(
            "Не указаны BYBIT_API_KEY / BYBIT_API_SECRET в .env. "
            "Создай read-only API ключ на Bybit и добавь его в .env."
        )
    exchange_cls = getattr(ccxt, settings.exchange_id)
    ex = exchange_cls({
        "apiKey": settings.bybit_api_key,
        "secret": settings.bybit_api_secret,
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


def private_call_with_time_retry(exchange, fn, *args, **kwargs):
    """Run a private Bybit call; resync time and retry once on timestamp errors."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if not _is_bybit_time_error(exc):
            raise
        sync_bybit_time(exchange, safety_ms=max(settings.bybit_time_safety_ms, 2500))
        return fn(*args, **kwargs)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def fetch_spot_balances(exchange=None) -> dict[str, dict]:
    """Read-only balances from Bybit. Returns only tracked coins + USDT with non-zero amounts."""
    ex = exchange or make_private_exchange()
    params = {"accountType": "UNIFIED", "recvWindow": int(settings.bybit_recv_window)}
    try:
        balance = private_call_with_time_retry(ex, ex.fetch_balance, params=params)
    except Exception:
        balance = private_call_with_time_retry(ex, ex.fetch_balance, params={"recvWindow": int(settings.bybit_recv_window)})

    result: dict[str, dict] = {}
    wanted = set(COINS + [settings.quote])
    for coin in wanted:
        row = balance.get(coin) or {}
        total = _safe_float(row.get("total"))
        free = _safe_float(row.get("free"))
        used = _safe_float(row.get("used"))
        if total > 1e-12 or free > 1e-12 or used > 1e-12:
            result[coin] = {"coin": coin, "total": total, "free": free, "used": used}
    return result


def calc_avg_from_trades(exchange, coin: str, limit: int = 1000) -> tuple[float, float, float, str]:
    coin = normalize_coin(coin)
    symbol = f"{coin}/{settings.quote}"
    raw_symbol = f"{coin}{settings.quote}"

    def calc_from_trade_list(trades: list[dict], source_name: str) -> tuple[float, float, float, str]:
        trades = sorted(trades or [], key=lambda t: t.get("timestamp") or t.get("execTime") or 0)

        qty = 0.0
        cost = 0.0

        for t in trades:
            side = (t.get("side") or "").lower()
            amount = _safe_float(t.get("amount") or t.get("execQty"))
            price = _safe_float(t.get("price") or t.get("execPrice"))
            trade_cost = _safe_float(
                t.get("cost") or t.get("execValue"),
                amount * price
            )

            if amount <= 0 or price <= 0:
                continue

            if side == "buy":
                qty += amount
                cost += trade_cost

            elif side == "sell" and qty > 0:
                sell_qty = min(amount, qty)
                avg = cost / qty if qty else 0.0
                qty -= sell_qty
                cost -= avg * sell_qty

                if qty <= 1e-12:
                    qty = 0.0
                    cost = 0.0

        avg_price = cost / qty if qty > 1e-12 else 0.0
        source = source_name if avg_price > 0 else "no_open_trade_cost"
        return avg_price, qty, cost, source

    # 1) Сначала пробуем стандартный ccxt fetch_my_trades
    all_trades = []

    for chunk in [200, 500, 1000]:
        try:
            trades = private_call_with_time_retry(
                exchange,
                exchange.fetch_my_trades,
                symbol,
                limit=chunk,
                params={
                    "category": "spot",
                    "recvWindow": int(settings.bybit_recv_window),
                },
            )
            if trades:
                all_trades = trades
        except Exception:
            continue

    if all_trades:
        avg_price, qty, cost, source = calc_from_trade_list(all_trades, "trades")
        if avg_price > 0:
            return avg_price, qty, cost, source

    # 2) Fallback: Bybit raw endpoint /v5/execution/list
    # Нужен, потому что Bybit иногда не отдаёт spot сделки через ccxt fetch_my_trades.
    raw_trades = []

    try:
        cursor = None

        for _ in range(5):  # максимум 5 страниц по 100 сделок
            params = {
                "category": "spot",
                "symbol": raw_symbol,
                "limit": 100,
                "recvWindow": int(settings.bybit_recv_window),
            }

            if cursor:
                params["cursor"] = cursor

            resp = private_call_with_time_retry(
                exchange,
                exchange.privateGetV5ExecutionList,
                params,
            )

            result = resp.get("result") or {}
            rows = result.get("list") or []

            if rows:
                raw_trades.extend(rows)

            cursor = result.get("nextPageCursor")

            if not cursor:
                break

    except Exception:
        raw_trades = []

    if raw_trades:
        avg_price, qty, cost, source = calc_from_trade_list(raw_trades, "bybit_execution")
        if avg_price > 0:
            return avg_price, qty, cost, source

    return 0.0, 0.0, 0.0, "no_trade_history"


def build_bybit_assets() -> tuple[list[BybitAsset], dict]:
    private_ex = make_private_exchange()
    public_ex = make_exchange()
    balances = fetch_spot_balances(private_ex)
    local_positions = get_open_positions()

    assets: list[BybitAsset] = []
    quote_balance = balances.get(settings.quote, {"total": 0.0, "free": 0.0, "used": 0.0})

    for coin in COINS:
        bal = balances.get(coin)
        if not bal:
            continue
        qty = _safe_float(bal.get("total"))
        if qty <= 1e-12:
            continue

        try:
            item = analyze_coin(public_ex, coin)
            current = _safe_float(item.get("current"))
        except Exception:
            current = 0.0

        avg, qty_from_trades, cost, source = calc_avg_from_trades(private_ex, coin)

        # If trade history is unavailable/incomplete, fall back to local journal average.
        local = local_positions.get(coin) or {}
        local_avg = _safe_float(local.get("avg_price"))
        local_invested = _safe_float(local.get("invested_usdt"))
        if avg <= 0 and local_avg > 0:
            avg = local_avg
            cost = qty * avg
            source = "local_journal"
        elif avg > 0:
            # Align cost to actual Bybit balance quantity. Trade qty can differ due to old history/fees.
            cost = qty * avg

        value = qty * current if current else 0.0
        pnl = value - cost if cost and value else 0.0
        pnl_pct = pnl / cost * 100 if cost else 0.0

        assets.append(BybitAsset(
            coin=coin,
            qty=qty,
            free=_safe_float(bal.get("free")),
            used=_safe_float(bal.get("used")),
            avg_price=avg,
            invested_usdt=cost,
            current_price=current,
            current_value=value,
            pnl_usdt=pnl,
            pnl_pct=pnl_pct,
            source=source,
        ))

    return assets, quote_balance


def _tp_hint(asset: BybitAsset) -> str:
    if asset.avg_price <= 0 or asset.current_price <= 0:
        return "TP: нет средней цены"
    cat = CATEGORIES.get(asset.coin, "STRONG_ALT")
    levels = get_tp_levels(asset.coin)
    reached = [lvl for lvl in levels if asset.pnl_pct >= lvl]
    if reached:
        lvl = max(reached)
        return f"🎯 TP: достигнут +{lvl:.0f}%, можно фиксировать часть"
    next_lvl = next((lvl for lvl in levels if asset.pnl_pct < lvl), levels[-1])
    target_price = asset.avg_price * (1 + next_lvl / 100)
    dist = (target_price / asset.current_price - 1) * 100 if asset.current_price else 0
    return f"TP: +{next_lvl:.0f}% ≈ {fmt_usdt(target_price)} USDT, до цели {dist:+.2f}%"


def _dca_hint(asset: BybitAsset, local_pos: dict | None = None) -> str:
    if not local_pos:
        return "DCA: синхронизируй, чтобы бот считал доборы"
    next_buy = _safe_float(local_pos.get("next_buy_price"))
    if next_buy <= 0 or asset.current_price <= 0:
        return "DCA: нет следующего уровня"
    dist = (asset.current_price / next_buy - 1) * 100
    if asset.current_price <= next_buy:
        return f"📉 DCA: достигнут уровень добора {fmt_usdt(next_buy)} USDT"
    return f"DCA: след. добор {fmt_usdt(next_buy)} USDT, осталось {dist:+.2f}%"


def format_bybit_portfolio(sync_hint: bool = True) -> str:
    assets, quote_balance = build_bybit_assets()
    local_positions = get_open_positions()
    lines = ["🔗 BYBIT PORTFOLIO — READ ONLY\n"]
    lines.append(f"USDT: total {fmt_money(quote_balance.get('total', 0))} | free {fmt_money(quote_balance.get('free', 0))}")
    if not assets:
        lines.append("\nОтслеживаемых монет на Bybit не найдено.")
        return "\n".join(lines)

    total_value = _safe_float(quote_balance.get("total"))
    total_cost = 0.0
    total_pnl = 0.0
    lines.append("")
    for a in assets:
        total_value += a.current_value
        total_cost += a.invested_usdt
        total_pnl += a.pnl_usdt
        emoji = "🟢" if a.pnl_pct >= 0 else "🔴"
        avg_text = fmt_usdt(a.avg_price) if a.avg_price else "нет данных"
        source_note = {
            "trades": "история Bybit",
            "local_journal": "локальный журнал",
            "no_trade_history": "нет истории",
            "no_open_trade_cost": "нет стоимости",
        }.get(a.source, a.source)
        lines.append(f"{emoji} {a.coin}")
        lines.append(f"  Кол-во: {a.qty:.8f}")
        lines.append(f"  Средняя: {avg_text} USDT ({source_note})")
        lines.append(f"  Сейчас: {fmt_usdt(a.current_price)} USDT")
        lines.append(f"  Стоимость: {fmt_money(a.current_value)}")
        if a.avg_price:
            lines.append(f"  PnL: {a.pnl_pct:+.2f}% / {fmt_money(a.pnl_usdt)}")
            lines.append(f"  {_tp_hint(a)}")
        lines.append(f"  {_dca_hint(a, local_positions.get(a.coin))}")
        lines.append("")

    lines.append("━━━━━━━━━━━━")
    lines.append(f"Оценка портфеля: {fmt_money(total_value)}")
    if total_cost:
        lines.append(f"PnL по монетам со средней: {fmt_money(total_pnl)}")
    if sync_hint:
        lines.append("\nКоманда /syncbybit обновит локальные позиции из Bybit. Нужен только read-only API.")
    return "\n".join(lines)


def sync_bybit_to_local() -> dict:
    """Overwrite local open positions with read-only Bybit balances + computed avg prices.

    Does not trade. It only updates local data/portfolio.json so DCA/TP can work from real holdings.
    """
    assets, quote_balance = build_bybit_assets()
    state = load_portfolio()
    positions = {}
    synced = []
    skipped = []

    for a in assets:
        if a.avg_price <= 0:
            skipped.append({"coin": a.coin, "reason": "no_avg_price"})
            continue

        # Build local position from Bybit reality.
        # We do not know exact number of DCA entries from trade history reliably, so estimate entries by local previous value or 1.
        old = (state.get("positions") or {}).get(a.coin, {})
        entry_count = int(old.get("entry_count") or 1)
        dca_levels = old.get("dca_levels_used") or []
        if not dca_levels:
            # Let storage static fallback handle this by using next_buy_drop_pct in old local if present.
            pass
        next_drop = _safe_float(old.get("next_buy_drop_pct"))
        if next_drop <= 0:
            from app.config import BASE_DCA_LEVELS
            cat = CATEGORIES.get(a.coin, "STRONG_ALT")
            levels = BASE_DCA_LEVELS.get(cat, [12, 16, 25, 35])
            idx = min(entry_count, len(levels) - 1)
            next_drop = levels[idx]
        next_buy_price = round(a.current_price * (1 - next_drop / 100), 8) if a.current_price else round(a.avg_price * (1 - next_drop / 100), 8)

        positions[a.coin] = {
            "coin": a.coin,
            "qty": a.qty,
            "invested_usdt": a.invested_usdt,
            "avg_price": a.avg_price,
            "entry_count": entry_count,
            "last_entry_price": a.current_price or a.avg_price,
            "next_buy_price": next_buy_price,
            "next_buy_drop_pct": next_drop,
            "take_profit_10_sent": False,
            "take_profit_15_sent": False,
            "entries": old.get("entries", []),
            "source": "bybit_sync",
            "synced_at": now_iso(),
        }
        if dca_levels:
            positions[a.coin]["dca_levels_used"] = dca_levels
        synced.append(a.coin)

    state["positions"] = positions
    state["bybit_quote_balance"] = quote_balance
    state["last_bybit_sync"] = now_iso()
    save_portfolio(state)

    add_journal({
        "action": "SYNC_BYBIT",
        "coin": "PORTFOLIO",
        "price": 0,
        "amount_usdt": 0,
        "qty": 0,
        "reason": f"synced={','.join(synced)} skipped={','.join(x['coin'] for x in skipped)}",
    })
    return {"synced": synced, "skipped": skipped, "quote_balance": quote_balance}
