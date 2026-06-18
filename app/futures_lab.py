from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt

from app.settings import settings
from app.storage import load_portfolio, update_portfolio

FUTURES_KEY = "futures_lab"
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
LEVERAGE = 5
VIRTUAL_USDT_START = 1000.0
RISK_PER_TRADE = 0.02
MAX_CONCURRENT = 1
TIMEFRAME = "1h"
CANDLE_LIMIT = 100
ATR_PERIOD = 14
ATR_STOP_MULT = 1.5
TP_RISK_RATIO = 2.0
BREAKOUT_PERIOD = 20
MAX_JOURNAL_ROWS = 2000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_futures_exchange():
    exchange_cls = getattr(ccxt, settings.exchange_id)
    return exchange_cls({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "linear",
            "adjustForTimeDifference": True,
            "recvWindow": settings.bybit_recv_window,
        },
    })


def _compute_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [float("nan")] * len(values)
    k = 2.0 / (period + 1)
    emas = [float("nan")] * len(values)
    emas[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        emas[i] = values[i] * k + emas[i - 1] * (1 - k)
    return emas


def _compute_atr(candles: list, period: int = ATR_PERIOD) -> list[float]:
    atrs = [float("nan")] * len(candles)
    if len(candles) < period + 1:
        return atrs

    trs: list[float] = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if len(trs) < period:
        return atrs

    atrs[period] = sum(trs[:period]) / period
    for i in range(period + 1, len(candles)):
        atrs[i] = (atrs[i - 1] * (period - 1) + trs[i - 1]) / period

    return atrs


def _liquidation_price(entry: float, side: str, leverage: int) -> float:
    if side == "long":
        return entry * (1.0 - 1.0 / leverage)
    return entry * (1.0 + 1.0 / leverage)


def _stop_is_safe(entry: float, stop: float, side: str, leverage: int) -> bool:
    liq = _liquidation_price(entry, side, leverage)
    if side == "long":
        return liq < stop < entry
    return entry < stop < liq


def _calc_position_size(deposit: float, entry: float, stop: float) -> float:
    stop_distance_pct = abs(entry - stop) / entry
    if stop_distance_pct <= 0:
        return 0.0
    return (deposit * RISK_PER_TRADE) / stop_distance_pct


def default_futures_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "virtual_usdt": VIRTUAL_USDT_START,
        "initial_usdt": VIRTUAL_USDT_START,
        "active_trade": None,
        "journal": [],
        "last_tick": None,
        "last_event": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def load_futures_state() -> dict[str, Any]:
    state = load_portfolio()
    lab = state.get(FUTURES_KEY)

    if not lab:
        lab = default_futures_state()
        # Only touch our own key so we never clobber positions/rotation_lab.
        update_portfolio(lambda s: s.update({FUTURES_KEY: lab}))
        return lab

    lab.setdefault("enabled", True)
    lab.setdefault("virtual_usdt", VIRTUAL_USDT_START)
    lab.setdefault("initial_usdt", VIRTUAL_USDT_START)
    lab.setdefault("active_trade", None)
    lab.setdefault("journal", [])
    lab.setdefault("last_tick", None)
    lab.setdefault("last_event", None)

    return lab


def save_futures_state(lab: dict[str, Any]) -> None:
    lab["updated_at"] = now_iso()
    # Atomic, key-scoped write: re-reads fresh state under the portfolio lock and
    # only replaces FUTURES_KEY, preserving positions and other labs' keys.
    update_portfolio(lambda s: s.update({FUTURES_KEY: lab}))


def _append_journal(lab: dict[str, Any], row: dict[str, Any]) -> None:
    lab.setdefault("journal", [])
    lab["journal"].append(row)
    lab["journal"] = lab["journal"][-MAX_JOURNAL_ROWS:]
    lab["last_event"] = row


# ─── Signal analysis ─────────────────────────────────────────────────────────

def _analyze_symbol(exchange, symbol: str) -> dict[str, Any] | None:
    candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
    if not candles or len(candles) < ATR_PERIOD + BREAKOUT_PERIOD + 5:
        return None

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    current = closes[-1]

    ema9_list = _compute_ema(closes, 9)
    ema30_list = _compute_ema(closes, 30)
    atrs = _compute_atr(candles, ATR_PERIOD)

    ema9 = ema9_list[-1]
    ema30 = ema30_list[-1]
    atr = atrs[-1]

    if any(math.isnan(x) for x in [ema9, ema30, atr]) or atr <= 0 or ema30 <= 0:
        return None

    # Exclude the last (current) candle from the breakout range
    high20 = max(highs[-BREAKOUT_PERIOD - 1:-1])
    low20 = min(lows[-BREAKOUT_PERIOD - 1:-1])

    return {
        "symbol": symbol,
        "current": current,
        "ema9": ema9,
        "ema30": ema30,
        "atr": atr,
        "high20": high20,
        "low20": low20,
    }


def _signal_trend_pullback(data: dict[str, Any]) -> dict[str, Any] | None:
    """Long in uptrend on pullback; short in downtrend on bounce."""
    current = data["current"]
    ema9 = data["ema9"]
    ema30 = data["ema30"]
    atr = data["atr"]

    # LONG: uptrend (EMA9 > EMA30), price pulled back below EMA9 but still above EMA30
    if ema9 > ema30 and ema30 < current < ema9:
        stop = current - ATR_STOP_MULT * atr
        if _stop_is_safe(current, stop, "long", LEVERAGE):
            return {
                "side": "long",
                "stop_price": stop,
                "entry_reason": {
                    "ema9": round(ema9, 4),
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "condition": "uptrend pullback: EMA30 < price < EMA9",
                },
            }

    # SHORT: downtrend (EMA9 < EMA30), price bounced above EMA9 but still below EMA30
    if ema9 < ema30 and ema9 < current < ema30:
        stop = current + ATR_STOP_MULT * atr
        if _stop_is_safe(current, stop, "short", LEVERAGE):
            return {
                "side": "short",
                "stop_price": stop,
                "entry_reason": {
                    "ema9": round(ema9, 4),
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "condition": "downtrend bounce: EMA9 < price < EMA30",
                },
            }

    return None


def _signal_mean_reversion(data: dict[str, Any]) -> dict[str, Any] | None:
    """Long when price deviates >2*ATR below EMA30; short when >2*ATR above."""
    current = data["current"]
    ema30 = data["ema30"]
    atr = data["atr"]
    deviation = current - ema30

    if deviation < -2.0 * atr:
        stop = current - ATR_STOP_MULT * atr
        if _stop_is_safe(current, stop, "long", LEVERAGE):
            return {
                "side": "long",
                "stop_price": stop,
                "entry_reason": {
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "deviation": round(deviation, 4),
                    "deviation_atr_mult": round(deviation / atr, 2),
                    "condition": "mean_reversion: price < EMA30 - 2*ATR",
                },
            }

    if deviation > 2.0 * atr:
        stop = current + ATR_STOP_MULT * atr
        if _stop_is_safe(current, stop, "short", LEVERAGE):
            return {
                "side": "short",
                "stop_price": stop,
                "entry_reason": {
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "deviation": round(deviation, 4),
                    "deviation_atr_mult": round(deviation / atr, 2),
                    "condition": "mean_reversion: price > EMA30 + 2*ATR",
                },
            }

    return None


def _signal_breakout(data: dict[str, Any]) -> dict[str, Any] | None:
    """Long on break above 20-candle high; short on break below 20-candle low."""
    current = data["current"]
    atr = data["atr"]
    high20 = data["high20"]
    low20 = data["low20"]

    if current > high20:
        stop = high20 - 0.5 * atr
        if _stop_is_safe(current, stop, "long", LEVERAGE):
            return {
                "side": "long",
                "stop_price": stop,
                "entry_reason": {
                    "high20": round(high20, 4),
                    "atr": round(atr, 4),
                    "breakout_pct": round((current / high20 - 1) * 100, 3),
                    "condition": f"breakout long: {current:.2f} > high20 {high20:.2f}",
                },
            }

    if current < low20:
        stop = low20 + 0.5 * atr
        if _stop_is_safe(current, stop, "short", LEVERAGE):
            return {
                "side": "short",
                "stop_price": stop,
                "entry_reason": {
                    "low20": round(low20, 4),
                    "atr": round(atr, 4),
                    "breakout_pct": round((1 - current / low20) * 100, 3),
                    "condition": f"breakdown short: {current:.2f} < low20 {low20:.2f}",
                },
            }

    return None


_STRATEGY_FINDERS = {
    "trend_pullback": _signal_trend_pullback,
    "mean_reversion": _signal_mean_reversion,
    "breakout": _signal_breakout,
}


def _scan_for_signals(exchange) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        try:
            data = _analyze_symbol(exchange, symbol)
            if not data:
                continue
            for strategy_name, finder in _STRATEGY_FINDERS.items():
                result = finder(data)
                if result:
                    signals.append({
                        "symbol": symbol,
                        "strategy": strategy_name,
                        "current": data["current"],
                        "atr": data["atr"],
                        **result,
                    })
        except Exception:
            continue
    return signals


# ─── Trade lifecycle ──────────────────────────────────────────────────────────

def _open_trade(lab: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
    deposit = float(lab["virtual_usdt"])
    entry_price = float(signal["current"])
    stop_price = float(signal["stop_price"])
    side = signal["side"]
    symbol = signal["symbol"]
    strategy = signal["strategy"]
    atr = float(signal.get("atr") or 0)

    position_size_usdt = _calc_position_size(deposit, entry_price, stop_price)
    liq_price = _liquidation_price(entry_price, side, LEVERAGE)

    risk_distance = abs(entry_price - stop_price)
    tp_price = (
        entry_price + TP_RISK_RATIO * risk_distance
        if side == "long"
        else entry_price - TP_RISK_RATIO * risk_distance
    )

    trade: dict[str, Any] = {
        "strategy": strategy,
        "side": side,
        "symbol": symbol,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "trailing_stop": stop_price,
        "trailing_activated": False,
        "take_profit_price": tp_price,
        "liquidation_price": liq_price,
        "position_size_usdt": position_size_usdt,
        "leverage": LEVERAGE,
        "deposit_before": deposit,
        "entry_reason": signal.get("entry_reason", {}),
        "atr": atr,
        "highest_price": entry_price,
        "lowest_price": entry_price,
        "opened_at": now_iso(),
    }

    lab["active_trade"] = trade

    _append_journal(lab, {
        "action": "OPEN",
        "time": now_iso(),
        "strategy": strategy,
        "side": side,
        "symbol": symbol,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "take_profit_price": tp_price,
        "liquidation_price": liq_price,
        "position_size_usdt": position_size_usdt,
        "leverage": LEVERAGE,
        "deposit_before": deposit,
        "entry_reason": signal.get("entry_reason", {}),
        "opened_at": trade["opened_at"],
    })

    return lab


def _close_trade(lab: dict[str, Any], exit_price: float, reason: str) -> dict[str, Any]:
    trade = lab.get("active_trade")
    if not trade:
        return lab

    deposit_before = float(trade["deposit_before"])
    entry_price = float(trade["entry_price"])
    position_size_usdt = float(trade["position_size_usdt"])
    side = trade["side"]

    if side == "long":
        pnl_usdt = position_size_usdt * (exit_price - entry_price) / entry_price
    else:
        pnl_usdt = position_size_usdt * (entry_price - exit_price) / entry_price

    pnl_pct = pnl_usdt / deposit_before * 100 if deposit_before else 0.0
    deposit_after = deposit_before + pnl_usdt
    lab["virtual_usdt"] = deposit_after

    duration_hours = 0.0
    try:
        opened_at = datetime.fromisoformat(trade["opened_at"])
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        duration_hours = round(
            (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600, 2
        )
    except Exception:
        pass

    closed_at = now_iso()

    _append_journal(lab, {
        "action": "CLOSE",
        "time": closed_at,
        "strategy": trade["strategy"],
        "side": side,
        "symbol": trade["symbol"],
        "entry_price": entry_price,
        "stop_price": trade["stop_price"],
        "take_profit_price": trade.get("take_profit_price"),
        "liquidation_price": trade.get("liquidation_price"),
        "position_size_usdt": position_size_usdt,
        "leverage": LEVERAGE,
        "entry_reason": trade.get("entry_reason", {}),
        "exit_price": exit_price,
        "exit_reason": reason,
        "pnl_pct": round(pnl_pct, 4),
        "pnl_usdt": round(pnl_usdt, 4),
        "duration_hours": duration_hours,
        "deposit_before": deposit_before,
        "deposit_after": round(deposit_after, 4),
        "opened_at": trade.get("opened_at"),
        "closed_at": closed_at,
    })

    lab["active_trade"] = None
    return lab


# ─── Main tick ────────────────────────────────────────────────────────────────

def futures_tick() -> dict[str, Any]:
    exchange = make_futures_exchange()
    lab = load_futures_state()
    lab["last_tick"] = now_iso()
    lab["last_event"] = None

    if not lab.get("enabled", True):
        save_futures_state(lab)
        return {"status": "disabled", "virtual_usdt": float(lab.get("virtual_usdt") or 0)}

    active = lab.get("active_trade")

    if active:
        symbol = active["symbol"]
        try:
            ticker = exchange.fetch_ticker(symbol)
            current_price = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception as exc:
            save_futures_state(lab)
            return {"status": "error", "error": str(exc)}

        if current_price <= 0:
            save_futures_state(lab)
            return {"status": "holding", "note": "price=0"}

        side = active["side"]
        entry_price = float(active["entry_price"])
        atr = float(active.get("atr") or 0)
        position_size_usdt = float(active["position_size_usdt"])
        deposit_before = float(active["deposit_before"])

        # Update trailing stop
        if atr > 0:
            if side == "long":
                highest = max(float(active.get("highest_price") or entry_price), current_price)
                active["highest_price"] = highest
                if current_price >= entry_price + ATR_STOP_MULT * atr:
                    active["trailing_activated"] = True
                if active.get("trailing_activated"):
                    new_trail = highest - ATR_STOP_MULT * atr
                    active["trailing_stop"] = max(
                        new_trail, float(active.get("trailing_stop") or active["stop_price"])
                    )
            else:
                lowest = min(float(active.get("lowest_price") or entry_price), current_price)
                active["lowest_price"] = lowest
                if current_price <= entry_price - ATR_STOP_MULT * atr:
                    active["trailing_activated"] = True
                if active.get("trailing_activated"):
                    new_trail = lowest + ATR_STOP_MULT * atr
                    active["trailing_stop"] = min(
                        new_trail, float(active.get("trailing_stop") or active["stop_price"])
                    )

        lab["active_trade"] = active

        effective_stop = float(active.get("trailing_stop") or active["stop_price"])
        tp_price = float(active.get("take_profit_price") or 0)
        liq_price = float(active.get("liquidation_price") or 0)

        # Check liquidation first (most critical)
        liq_hit = (side == "long" and liq_price > 0 and current_price <= liq_price) or (
            side == "short" and liq_price > 0 and current_price >= liq_price
        )
        if liq_hit:
            lab = _close_trade(lab, liq_price, "liquidation")
            save_futures_state(lab)
            last = lab["journal"][-1] if lab["journal"] else {}
            return {
                "status": "closed",
                "reason": "liquidation",
                "symbol": symbol,
                "pnl_usdt": last.get("pnl_usdt"),
                "virtual_usdt": float(lab["virtual_usdt"]),
                "event": lab.get("last_event"),
            }

        # Check stop
        stop_hit = (side == "long" and current_price <= effective_stop) or (
            side == "short" and current_price >= effective_stop
        )
        if stop_hit:
            reason = "trailing_stop" if active.get("trailing_activated") else "stop"
            lab = _close_trade(lab, current_price, reason)
            save_futures_state(lab)
            last = lab["journal"][-1] if lab["journal"] else {}
            return {
                "status": "closed",
                "reason": reason,
                "symbol": symbol,
                "pnl_usdt": last.get("pnl_usdt"),
                "virtual_usdt": float(lab["virtual_usdt"]),
                "event": lab.get("last_event"),
            }

        # Check take profit
        tp_hit = (side == "long" and tp_price > 0 and current_price >= tp_price) or (
            side == "short" and tp_price > 0 and current_price <= tp_price
        )
        if tp_hit:
            lab = _close_trade(lab, current_price, "take")
            save_futures_state(lab)
            last = lab["journal"][-1] if lab["journal"] else {}
            return {
                "status": "closed",
                "reason": "take",
                "symbol": symbol,
                "pnl_usdt": last.get("pnl_usdt"),
                "virtual_usdt": float(lab["virtual_usdt"]),
                "event": lab.get("last_event"),
            }

        save_futures_state(lab)

        if side == "long":
            unrealized_pnl = position_size_usdt * (current_price - entry_price) / entry_price
        else:
            unrealized_pnl = position_size_usdt * (entry_price - current_price) / entry_price

        return {
            "status": "holding",
            "strategy": active["strategy"],
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "current_price": current_price,
            "stop": effective_stop,
            "tp": tp_price,
            "trailing_activated": active.get("trailing_activated", False),
            "unrealized_pnl_usdt": round(unrealized_pnl, 2),
            "unrealized_pct": round(unrealized_pnl / deposit_before * 100, 2) if deposit_before else 0,
            "virtual_usdt": float(lab["virtual_usdt"]),
        }

    # No active trade — scan for entry signals
    signals = _scan_for_signals(exchange)

    if signals:
        signal = signals[0]
        lab = _open_trade(lab, signal)
        save_futures_state(lab)
        return {
            "status": "opened",
            "strategy": signal["strategy"],
            "symbol": signal["symbol"],
            "side": signal["side"],
            "entry_price": signal["current"],
            "stop_price": signal["stop_price"],
            "virtual_usdt": float(lab["virtual_usdt"]),
            "event": lab.get("last_event"),
        }

    save_futures_state(lab)
    return {
        "status": "idle",
        "virtual_usdt": float(lab.get("virtual_usdt") or 0),
        "signals_checked": len(SYMBOLS) * len(_STRATEGY_FINDERS),
    }


# ─── Public reporting ─────────────────────────────────────────────────────────

def _per_strategy_stats(closed: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for row in closed:
        s = row.get("strategy") or "unknown"
        if s not in stats:
            stats[s] = {"count": 0, "wins": 0, "losses": 0, "pnl_usdt": 0.0}
        stats[s]["count"] += 1
        pnl = float(row.get("pnl_usdt") or 0)
        stats[s]["pnl_usdt"] += pnl
        if pnl > 0:
            stats[s]["wins"] += 1
        else:
            stats[s]["losses"] += 1
    for s in stats:
        cnt = stats[s]["count"]
        stats[s]["winrate"] = stats[s]["wins"] / cnt * 100 if cnt else 0.0
    return stats


def futures_summary() -> str:
    lab = load_futures_state()
    initial = float(lab.get("initial_usdt") or VIRTUAL_USDT_START)
    current = float(lab.get("virtual_usdt") or VIRTUAL_USDT_START)
    growth_pct = (current / initial - 1) * 100 if initial else 0.0

    journal = lab.get("journal") or []
    closed = [x for x in journal if x.get("action") == "CLOSE"]
    wins = [x for x in closed if float(x.get("pnl_usdt") or 0) > 0]
    winrate = len(wins) / len(closed) * 100 if closed else 0.0

    per_strat = _per_strategy_stats(closed)

    lines = [
        "📈 FUTURES LAB (виртуальный)",
        "",
        f"Включён: {'да' if lab.get('enabled', True) else 'нет'}",
        f"Таймфрейм: {TIMEFRAME} | Плечо: {LEVERAGE}x | Риск/сделка: {RISK_PER_TRADE*100:.0f}%",
        f"Пары: {', '.join(SYMBOLS)}",
        f"Депо старт: ${initial:.2f}",
        f"Депо сейчас: ${current:.2f}",
        f"Результат: {growth_pct:+.2f}%",
        "",
        f"Закрытых сделок: {len(closed)}",
        f"Плюсовых: {len(wins)} | Минусовых: {len(closed) - len(wins)}",
        f"Общий винрейт: {winrate:.1f}%",
        f"Последний тик: {lab.get('last_tick') or 'нет'}",
    ]

    if per_strat:
        lines += ["", "Винрейт по стратегиям:"]
        for name in ("trend_pullback", "mean_reversion", "breakout"):
            st = per_strat.get(name)
            if st:
                lines.append(
                    f"  {name}: {st['count']} сделок | "
                    f"WR {st['winrate']:.1f}% | "
                    f"PnL ${st['pnl_usdt']:+.2f}"
                )

    active = lab.get("active_trade")
    if active:
        lines += [
            "",
            "Открытая позиция:",
            f"  Стратегия: {active['strategy']}",
            f"  Пара: {active['symbol']} | Сторона: {active['side'].upper()}",
            f"  Вход: {active['entry_price']} | Стоп: {float(active.get('trailing_stop') or active['stop_price']):.2f}",
            f"  TP: {active.get('take_profit_price', '?')} | Лик: {active.get('liquidation_price', '?'):.2f}",
            f"  Позиция: ${float(active['position_size_usdt']):.2f} (нотионал) | Плечо: {LEVERAGE}x",
            f"  Трейлинг: {'активен' if active.get('trailing_activated') else 'ждёт'}",
            f"  Открыта: {active.get('opened_at')}",
        ]
    else:
        lines += ["", "Открытая позиция: нет"]

    return "\n".join(lines)


def _filter_closed_by_days(journal: list, days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for row in journal:
        if row.get("action") != "CLOSE":
            continue
        try:
            t_str = row.get("closed_at") or row.get("time") or ""
            t = datetime.fromisoformat(t_str)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t >= cutoff:
                result.append(row)
        except Exception:
            continue
    return result


def futures_report(days: int = 7) -> str:
    lab = load_futures_state()
    journal = lab.get("journal") or []
    closed = _filter_closed_by_days(journal, days)

    if not closed:
        return f"📊 FUTURES REPORT ({days}д)\n\nСделок за период нет."

    per_strat = _per_strategy_stats(closed)
    total_pnl = sum(float(r.get("pnl_usdt") or 0) for r in closed)
    total_wins = sum(1 for r in closed if float(r.get("pnl_usdt") or 0) > 0)
    total_wr = total_wins / len(closed) * 100

    lines = [
        f"📊 FUTURES REPORT ({days}д)",
        f"Всего сделок: {len(closed)} | Винрейт: {total_wr:.1f}% | PnL: ${total_pnl:+.2f}",
        "",
    ]

    for name in ("trend_pullback", "mean_reversion", "breakout"):
        st = per_strat.get(name)
        if not st:
            lines.append(f"▪ {name}: нет сделок")
            continue
        lines.append(
            f"▪ {name}\n"
            f"  Сделок: {st['count']} | WR: {st['winrate']:.1f}%\n"
            f"  PnL: ${st['pnl_usdt']:+.2f} | "
            f"W:{st['wins']} / L:{st['losses']}"
        )

    # Top/worst trades
    sorted_trades = sorted(closed, key=lambda x: float(x.get("pnl_usdt") or 0), reverse=True)
    top3 = sorted_trades[:3]
    worst3 = list(reversed(sorted_trades[-3:]))

    lines += ["", "Топ-3 лучших:"]
    for i, t in enumerate(top3, 1):
        lines.append(
            f"  {i}. {t.get('symbol')} {t.get('side')} [{t.get('strategy')}] "
            f"${float(t.get('pnl_usdt') or 0):+.2f} | {t.get('exit_reason')}"
        )

    lines += ["", "Топ-3 худших:"]
    for i, t in enumerate(worst3, 1):
        lines.append(
            f"  {i}. {t.get('symbol')} {t.get('side')} [{t.get('strategy')}] "
            f"${float(t.get('pnl_usdt') or 0):+.2f} | {t.get('exit_reason')}"
        )

    return "\n".join(lines)


def export_futures_csv(days: int = 30) -> Path:
    lab = load_futures_state()
    journal = lab.get("journal") or []
    closed = _filter_closed_by_days(journal, days)

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"futures_{days}d_{ts}.csv"

    fieldnames = [
        "strategy", "side", "symbol",
        "entry_price", "stop_price", "take_profit_price", "liquidation_price",
        "position_size_usdt", "leverage",
        "exit_price", "exit_reason",
        "pnl_pct", "pnl_usdt",
        "duration_hours", "deposit_before", "deposit_after",
        "opened_at", "closed_at",
        "er_condition", "er_ema9", "er_ema30", "er_atr", "er_deviation",
        "er_high20", "er_low20", "er_breakout_pct",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in closed:
            er = row.get("entry_reason") or {}
            writer.writerow({
                "strategy": row.get("strategy"),
                "side": row.get("side"),
                "symbol": row.get("symbol"),
                "entry_price": row.get("entry_price"),
                "stop_price": row.get("stop_price"),
                "take_profit_price": row.get("take_profit_price"),
                "liquidation_price": row.get("liquidation_price"),
                "position_size_usdt": row.get("position_size_usdt"),
                "leverage": row.get("leverage"),
                "exit_price": row.get("exit_price"),
                "exit_reason": row.get("exit_reason"),
                "pnl_pct": row.get("pnl_pct"),
                "pnl_usdt": row.get("pnl_usdt"),
                "duration_hours": row.get("duration_hours"),
                "deposit_before": row.get("deposit_before"),
                "deposit_after": row.get("deposit_after"),
                "opened_at": row.get("opened_at"),
                "closed_at": row.get("closed_at"),
                "er_condition": er.get("condition"),
                "er_ema9": er.get("ema9"),
                "er_ema30": er.get("ema30"),
                "er_atr": er.get("atr"),
                "er_deviation": er.get("deviation"),
                "er_high20": er.get("high20"),
                "er_low20": er.get("low20"),
                "er_breakout_pct": er.get("breakout_pct"),
            })

    return path


def futures_reset() -> str:
    fresh = default_futures_state()
    update_portfolio(lambda s: s.update({FUTURES_KEY: fresh}))
    return f"📈 Futures Lab сброшен. Виртуальный баланс: ${VIRTUAL_USDT_START:.2f} USDT"


def futures_set_enabled(enabled: bool) -> str:
    lab = load_futures_state()
    lab["enabled"] = bool(enabled)
    save_futures_state(lab)
    return "📈 Futures Lab включён" if enabled else "📈 Futures Lab выключен"


def format_futures_event(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None

    action = event.get("action")
    symbol = event.get("symbol", "?")

    if action == "OPEN":
        er = event.get("entry_reason") or {}
        return (
            "📈 FUTURES DEMO ENTRY\n\n"
            f"Пара: {symbol} | {event.get('side', '').upper()}\n"
            f"Стратегия: {event.get('strategy')}\n"
            f"Вход: {event.get('entry_price')}\n"
            f"Стоп: {event.get('stop_price')}\n"
            f"TP: {event.get('take_profit_price')}\n"
            f"Лик: {float(event.get('liquidation_price') or 0):.2f}\n"
            f"Позиция: ${float(event.get('position_size_usdt') or 0):.2f} | x{LEVERAGE}\n"
            f"Условие: {er.get('condition', '')}\n"
            f"Депо: ${float(event.get('deposit_before') or 0):.2f}"
        )

    if action == "CLOSE":
        pnl = float(event.get("pnl_usdt") or 0)
        pnl_pct = float(event.get("pnl_pct") or 0)
        dep_before = float(event.get("deposit_before") or 0)
        dep_after = float(event.get("deposit_after") or 0)
        return (
            "📈 FUTURES DEMO EXIT\n\n"
            f"Пара: {symbol} | {event.get('side', '').upper()}\n"
            f"Стратегия: {event.get('strategy')}\n"
            f"Причина: {event.get('exit_reason')}\n"
            f"Вход: {event.get('entry_price')} → Выход: {event.get('exit_price')}\n"
            f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}% депо)\n"
            f"Длительность: {event.get('duration_hours')}ч\n"
            f"Депо: ${dep_before:.2f} → ${dep_after:.2f}"
        )

    return None
