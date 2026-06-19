"""PRO futures simulator — trend-following with pyramiding (competitor to futures_lab).

Same conditions as ``futures_lab`` (virtual $1000 deposit, x5 leverage, paper
simulation) but a different philosophy: instead of fading pullbacks on a small
timeframe, PRO follows the higher-timeframe (4h) trend, enters continuation
pullbacks on 1h, pyramids INTO winners (never averages down a loser), uses a
funding-rate sentiment filter, takes 50% off at +2 ATR (stop to breakeven) and
trails the rest by 3 ATR.

State lives under the ``pro_lab`` key in ``portfolio_state.json`` and is written
through ``update_portfolio`` so it shares the same flock and never clobbers
positions / futures_lab / rotation_lab keys.

Circuit-breaker logic (daily-loss limit, consecutive-loss cooldown, per-symbol
stop cooldown) is reused from ``futures_lab`` so both simulators play by the
exact same risk rules — only the entry/management philosophy differs.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt

from app.settings import settings
from app.storage import load_portfolio, update_portfolio

# Reuse the shared maths + circuit-breaker primitives from futures_lab so the
# two simulators are risk-identical. We never mutate futures_lab — only import.
from app.futures_lab import (
    _compute_ema,
    _compute_atr,
    _liquidation_price,
    _stop_is_safe,
    _calc_position_size,
    _current_day_loss_pct,
    _roll_day,
    _global_entry_block,
    _parse_iso,
    DAILY_LOSS_LIMIT_PCT,
    MAX_CONSECUTIVE_LOSSES,
    COOLDOWN_AFTER_LOSSES_HOURS,
    STOP_COOLDOWN_HOURS,
)

PRO_KEY = "pro_lab"

# Top-10 USDT perpetuals.
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
    "LINK/USDT:USDT", "DOT/USDT:USDT",
]

LEVERAGE = 5
VIRTUAL_USDT_START = 1000.0
MAX_CONCURRENT = 1                 # one symbol at a time (adds pyramid the same position)

# ─── Risk ──────────────────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT = 1.5           # % of deposit risked on the first entry
INITIAL_STOP_ATR = 2.0             # initial protective stop distance, in 1h-ATR

# ─── Trend / entry ───────────────────────────────────────────────────────────
HTF_TIMEFRAME = "4h"
LTF_TIMEFRAME = "1h"
HTF_LIMIT = 250                    # enough for EMA200 on 4h
LTF_LIMIT = 100
EMA_FAST_HTF = 50
EMA_SLOW_HTF = 200
EMA_FAST_LTF = 9
EMA_SLOW_LTF = 30
ATR_PERIOD = 14

# ─── Pyramiding (add only into profit) ─────────────────────────────────────────
ADD_TRIGGER_ATR = 1.0              # add after price advances +1 ATR from the last add
ADD_FRACTION = 0.5                 # each add = 50% of the initial notional
MAX_ADDS = 2                       # up to 2 adds → 3 entries total

# ─── Position management ───────────────────────────────────────────────────────
PARTIAL_TP_ATR = 2.0               # take 50% off at +2 ATR from average entry
PARTIAL_TP_FRACTION = 0.5          # fraction closed at the partial take-profit
TRAIL_ATR = 3.0                    # trail the remainder by 3 ATR (let the trend run)

# ─── Funding sentiment filter ──────────────────────────────────────────────────
# funding expressed in percent (rate * 100), matching derivatives.classify_funding.
FUNDING_HOT_PCT = 0.08             # >= this → longs overheated, block new longs
FUNDING_COLD_PCT = -0.03           # <= this → market fearful, block new shorts

MAX_JOURNAL_ROWS = 2000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _base_coin(symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC'."""
    return symbol.split("/")[0]


# ─── State ───────────────────────────────────────────────────────────────────

def default_pro_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "virtual_usdt": VIRTUAL_USDT_START,
        "initial_usdt": VIRTUAL_USDT_START,
        "active_trade": None,
        "journal": [],
        "last_tick": None,
        "last_event": None,
        # Circuit-breaker state (same keys/semantics as futures_lab).
        "day_start_date": _today_utc(),
        "day_start_balance": VIRTUAL_USDT_START,
        "daily_limit_notified_date": None,
        "consecutive_losses": 0,
        "losses_cooldown_until": None,
        "stop_cooldowns": {},
        "blocked_entries": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def load_pro_state() -> dict[str, Any]:
    state = load_portfolio()
    lab = state.get(PRO_KEY)

    if not lab:
        lab = default_pro_state()
        update_portfolio(lambda s: s.update({PRO_KEY: lab}))
        return lab

    lab.setdefault("enabled", True)
    lab.setdefault("virtual_usdt", VIRTUAL_USDT_START)
    lab.setdefault("initial_usdt", VIRTUAL_USDT_START)
    lab.setdefault("active_trade", None)
    lab.setdefault("journal", [])
    lab.setdefault("last_tick", None)
    lab.setdefault("last_event", None)
    lab.setdefault("day_start_date", _today_utc())
    lab.setdefault("day_start_balance", float(lab.get("virtual_usdt") or VIRTUAL_USDT_START))
    lab.setdefault("daily_limit_notified_date", None)
    lab.setdefault("consecutive_losses", 0)
    lab.setdefault("losses_cooldown_until", None)
    lab.setdefault("stop_cooldowns", {})
    lab.setdefault("blocked_entries", [])
    return lab


def save_pro_state(lab: dict[str, Any]) -> None:
    lab["updated_at"] = now_iso()
    update_portfolio(lambda s: s.update({PRO_KEY: lab}))


def _append_journal(lab: dict[str, Any], row: dict[str, Any]) -> None:
    lab.setdefault("journal", [])
    lab["journal"].append(row)
    lab["journal"] = lab["journal"][-MAX_JOURNAL_ROWS:]
    lab["last_event"] = row


def _log_blocked_entry(lab: dict[str, Any], symbol: str, side: str, reason: str) -> None:
    lab.setdefault("blocked_entries", [])
    lab["blocked_entries"].append({
        "time": now_iso(),
        "symbol": symbol,
        "side": side,
        "strategy": "pro_trend",
        "reason": reason,
    })
    lab["blocked_entries"] = lab["blocked_entries"][-MAX_JOURNAL_ROWS:]


# ─── Exchange / market data ─────────────────────────────────────────────────

def make_pro_exchange():
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


def _funding_pct(exchange, symbol: str) -> float | None:
    """Current funding rate as a percentage (rate * 100), or None if unavailable."""
    try:
        fr = exchange.fetch_funding_rate(symbol)
        rate = fr.get("fundingRate")
        if rate is None:
            info = fr.get("info") or {}
            rate = info.get("fundingRate")
        if rate is None:
            return None
        return float(rate) * 100.0
    except Exception:
        return None


def _analyze_symbol(exchange, symbol: str) -> dict[str, Any] | None:
    """Higher-timeframe trend (4h EMA50/EMA200) + lower-timeframe entry data (1h)."""
    htf = exchange.fetch_ohlcv(symbol, timeframe=HTF_TIMEFRAME, limit=HTF_LIMIT)
    if not htf or len(htf) < EMA_SLOW_HTF + 5:
        return None
    htf_closes = [float(c[4]) for c in htf]
    htf_price = htf_closes[-1]
    ema_fast_htf = _compute_ema(htf_closes, EMA_FAST_HTF)[-1]
    ema_slow_htf = _compute_ema(htf_closes, EMA_SLOW_HTF)[-1]
    if math.isnan(ema_fast_htf) or math.isnan(ema_slow_htf):
        return None
    htf_trend = _classify_htf_trend(htf_price, ema_fast_htf, ema_slow_htf)

    ltf = exchange.fetch_ohlcv(symbol, timeframe=LTF_TIMEFRAME, limit=LTF_LIMIT)
    if not ltf or len(ltf) < ATR_PERIOD + 5:
        return None
    closes = [float(c[4]) for c in ltf]
    price = closes[-1]
    ema9 = _compute_ema(closes, EMA_FAST_LTF)[-1]
    ema30 = _compute_ema(closes, EMA_SLOW_LTF)[-1]
    atr = _compute_atr(ltf, ATR_PERIOD)[-1]
    if any(math.isnan(x) for x in (ema9, ema30, atr)) or atr <= 0 or ema30 <= 0:
        return None

    return {
        "symbol": symbol,
        "htf_trend": htf_trend,
        "ema_fast_4h": ema_fast_htf,
        "ema_slow_4h": ema_slow_htf,
        "price": price,
        "ema9": ema9,
        "ema30": ema30,
        "atr": atr,
    }


# ─── Pure decision functions (unit-tested offline) ─────────────────────────────

def _classify_htf_trend(price: float, ema_fast: float, ema_slow: float) -> str:
    """4h trend: 'up' / 'down' / 'none' from EMA50 vs EMA200 and price location."""
    if any(math.isnan(x) for x in (price, ema_fast, ema_slow)):
        return "none"
    if ema_fast > ema_slow and price > ema_fast:
        return "up"
    if ema_fast < ema_slow and price < ema_fast:
        return "down"
    return "none"


def _funding_blocks(side: str, funding_pct: float | None) -> bool:
    """Sentiment filter: block new longs when funding is overheated, new shorts when fearful."""
    if funding_pct is None:
        return False
    if side == "long" and funding_pct >= FUNDING_HOT_PCT:
        return True
    if side == "short" and funding_pct <= FUNDING_COLD_PCT:
        return True
    return False


def _entry_signal(data: dict[str, Any]) -> dict[str, Any] | None:
    """Trend-aligned continuation pullback entry. Returns None if not aligned with 4h trend.

    LONG  (4h up):   1h still up (EMA9>EMA30), price pulled back into [EMA30, EMA9].
    SHORT (4h down): 1h still down (EMA9<EMA30), price bounced into [EMA9, EMA30].
    """
    trend = data.get("htf_trend")
    price = data["price"]
    ema9 = data["ema9"]
    ema30 = data["ema30"]
    atr = data["atr"]

    if trend == "up":
        if ema9 > ema30 and ema30 <= price <= ema9:
            stop = price - INITIAL_STOP_ATR * atr
            if _stop_is_safe(price, stop, "long", LEVERAGE):
                return _make_signal(data, "long", price, stop,
                                    "uptrend(4h) pullback: EMA30 ≤ price ≤ EMA9 (1h)")
    elif trend == "down":
        if ema9 < ema30 and ema9 <= price <= ema30:
            stop = price + INITIAL_STOP_ATR * atr
            if _stop_is_safe(price, stop, "short", LEVERAGE):
                return _make_signal(data, "short", price, stop,
                                    "downtrend(4h) bounce: EMA9 ≤ price ≤ EMA30 (1h)")
    return None


def _make_signal(data: dict[str, Any], side: str, price: float, stop: float, condition: str) -> dict[str, Any]:
    return {
        "symbol": data["symbol"],
        "strategy": "pro_trend",
        "side": side,
        "entry_price": price,
        "stop_price": stop,
        "atr": data["atr"],
        "htf_trend": data.get("htf_trend"),
        "entry_reason": {
            "htf_trend": data.get("htf_trend"),
            "ema_fast_4h": round(float(data.get("ema_fast_4h") or 0), 6),
            "ema_slow_4h": round(float(data.get("ema_slow_4h") or 0), 6),
            "ema9_1h": round(float(data.get("ema9") or 0), 6),
            "ema30_1h": round(float(data.get("ema30") or 0), 6),
            "atr": round(float(data.get("atr") or 0), 6),
            "condition": condition,
        },
    }


def _settle_pnl(side: str, avg_entry: float, exit_price: float, notional: float) -> float:
    """PnL in USDT for closing ``notional`` of a position opened at ``avg_entry``."""
    if avg_entry <= 0:
        return 0.0
    move = (exit_price - avg_entry) / avg_entry if side == "long" else (avg_entry - exit_price) / avg_entry
    return notional * move


def _can_pyramid(trade: dict[str, Any], price: float) -> bool:
    """Add only INTO profit: in the green vs avg entry AND advanced +1 ATR from last add.

    Never adds once the partial take-profit has fired, and never beyond MAX_ADDS.
    A losing position is never averaged down.
    """
    if int(trade.get("adds") or 0) >= int(trade.get("max_adds") or MAX_ADDS):
        return False
    if trade.get("partial_done"):
        return False
    atr = float(trade.get("atr") or 0)
    if atr <= 0:
        return False
    side = trade["side"]
    avg = float(trade["avg_entry_price"])
    last = float(trade.get("last_add_price") or trade["avg_entry_price"])
    in_profit = (price > avg) if side == "long" else (price < avg)
    advanced = (price - last) >= ADD_TRIGGER_ATR * atr if side == "long" else (last - price) >= ADD_TRIGGER_ATR * atr
    return in_profit and advanced


def _apply_add(trade: dict[str, Any], price: float) -> dict[str, Any]:
    """Pyramid: add ADD_FRACTION of the initial notional, re-average, tighten the stop."""
    side = trade["side"]
    atr = float(trade.get("atr") or 0)
    add_notional = float(trade["initial_notional"]) * ADD_FRACTION
    old = float(trade["position_size_usdt"])
    old_avg = float(trade["avg_entry_price"])
    new_total = old + add_notional
    new_avg = (old * old_avg + add_notional * price) / new_total if new_total else old_avg

    trade["position_size_usdt"] = new_total
    trade["avg_entry_price"] = new_avg
    trade["entry_price"] = new_avg
    trade["adds"] = int(trade.get("adds") or 0) + 1
    trade["last_add_price"] = price

    new_stop = price - INITIAL_STOP_ATR * atr if side == "long" else price + INITIAL_STOP_ATR * atr
    cur_stop = float(trade.get("trailing_stop") or trade["stop_price"])
    trade["trailing_stop"] = max(cur_stop, new_stop) if side == "long" else min(cur_stop, new_stop)
    trade["liquidation_price"] = _liquidation_price(new_avg, side, LEVERAGE)
    return trade


def _apply_partial_tp(lab: dict[str, Any], trade: dict[str, Any], price: float) -> float:
    """Take PARTIAL_TP_FRACTION off at +2 ATR, realize PnL, move stop to breakeven."""
    side = trade["side"]
    avg = float(trade["avg_entry_price"])
    notional = float(trade["position_size_usdt"])
    chunk = notional * PARTIAL_TP_FRACTION
    pnl = _settle_pnl(side, avg, price, chunk)

    lab["virtual_usdt"] = float(lab["virtual_usdt"]) + pnl
    trade["realized_pnl_usdt"] = float(trade.get("realized_pnl_usdt") or 0) + pnl
    trade["position_size_usdt"] = notional - chunk
    trade["partial_done"] = True
    trade["trailing_activated"] = True
    # Stop to breakeven (average entry), never loosen.
    trade["stop_price"] = avg
    cur = float(trade.get("trailing_stop") or avg)
    trade["trailing_stop"] = max(cur, avg) if side == "long" else min(cur, avg)

    _append_journal(lab, {
        "action": "PARTIAL_TP",
        "time": now_iso(),
        "strategy": trade["strategy"],
        "side": side,
        "symbol": trade["symbol"],
        "fraction": PARTIAL_TP_FRACTION,
        "exit_price": price,
        "avg_entry_price": avg,
        "pnl_usdt": round(pnl, 4),
        "note": "fixed 50% at +2 ATR, stop → breakeven",
    })
    return pnl


def _open_trade(lab: dict[str, Any], signal: dict[str, Any], funding_pct: float | None) -> dict[str, Any]:
    deposit = float(lab["virtual_usdt"])
    side = signal["side"]
    symbol = signal["symbol"]
    entry = float(signal["entry_price"])
    stop = float(signal["stop_price"])
    atr = float(signal.get("atr") or 0)

    notional = _calc_position_size(deposit, entry, stop, RISK_PER_TRADE_PCT)
    liq = _liquidation_price(entry, side, LEVERAGE)

    entry_reason = dict(signal.get("entry_reason") or {})
    entry_reason["funding_pct"] = round(funding_pct, 5) if funding_pct is not None else None

    trade: dict[str, Any] = {
        "strategy": "pro_trend",
        "side": side,
        "symbol": symbol,
        "entry_price": entry,
        "initial_entry_price": entry,
        "avg_entry_price": entry,
        "stop_price": stop,
        "trailing_stop": stop,
        "trailing_activated": False,
        "liquidation_price": liq,
        "position_size_usdt": notional,
        "initial_notional": notional,
        "leverage": LEVERAGE,
        "atr": atr,
        "adds": 0,
        "max_adds": MAX_ADDS,
        "partial_done": False,
        "realized_pnl_usdt": 0.0,
        "highest_price": entry,
        "lowest_price": entry,
        "last_add_price": entry,
        "deposit_before": deposit,
        "entry_reason": entry_reason,
        "market_regime": signal.get("htf_trend"),
        "funding_pct": round(funding_pct, 5) if funding_pct is not None else None,
        "opened_at": now_iso(),
    }
    lab["active_trade"] = trade

    _append_journal(lab, {
        "action": "OPEN",
        "time": now_iso(),
        "strategy": "pro_trend",
        "side": side,
        "symbol": symbol,
        "entry_price": entry,
        "stop_price": stop,
        "liquidation_price": liq,
        "position_size_usdt": notional,
        "leverage": LEVERAGE,
        "deposit_before": deposit,
        "entry_reason": entry_reason,
        "market_regime": signal.get("htf_trend"),
        "funding_pct": entry_reason.get("funding_pct"),
        "opened_at": trade["opened_at"],
    })
    return lab


def _close_trade(lab: dict[str, Any], exit_price: float, reason: str,
                 notifications: list[str] | None = None) -> dict[str, Any]:
    trade = lab.get("active_trade")
    if not trade:
        return lab

    side = trade["side"]
    avg = float(trade["avg_entry_price"])
    notional = float(trade["position_size_usdt"])
    final_pnl = _settle_pnl(side, avg, exit_price, notional)
    lab["virtual_usdt"] = float(lab["virtual_usdt"]) + final_pnl

    partial_pnl = float(trade.get("realized_pnl_usdt") or 0)
    total_pnl = partial_pnl + final_pnl
    deposit_before = float(trade["deposit_before"])
    pnl_pct = total_pnl / deposit_before * 100 if deposit_before else 0.0
    deposit_after = float(lab["virtual_usdt"])

    duration_hours = 0.0
    try:
        opened_at = datetime.fromisoformat(trade["opened_at"])
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        duration_hours = round((datetime.now(timezone.utc) - opened_at).total_seconds() / 3600, 2)
    except Exception:
        pass

    closed_at = now_iso()
    _append_journal(lab, {
        "action": "CLOSE",
        "time": closed_at,
        "strategy": "pro_trend",
        "side": side,
        "symbol": trade["symbol"],
        "entry_price": float(trade.get("initial_entry_price") or avg),
        "avg_entry_price": avg,
        "stop_price": trade["stop_price"],
        "liquidation_price": trade.get("liquidation_price"),
        "position_size_usdt": notional,
        "initial_notional": trade.get("initial_notional"),
        "adds": int(trade.get("adds") or 0),
        "leverage": LEVERAGE,
        "entry_reason": trade.get("entry_reason", {}),
        "market_regime": trade.get("market_regime"),
        "funding_pct": trade.get("funding_pct"),
        "exit_price": exit_price,
        "exit_reason": reason,
        "partial_pnl_usdt": round(partial_pnl, 4),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_usdt": round(total_pnl, 4),
        "duration_hours": duration_hours,
        "deposit_before": deposit_before,
        "deposit_after": round(deposit_after, 4),
        "opened_at": trade.get("opened_at"),
        "closed_at": closed_at,
    })

    # ── Circuit-breaker bookkeeping (on the total realized trade PnL) ──
    now = datetime.now(timezone.utc)
    if total_pnl > 0:
        lab["consecutive_losses"] = 0
    else:
        streak = int(lab.get("consecutive_losses") or 0) + 1
        lab["consecutive_losses"] = streak
        if streak >= MAX_CONSECUTIVE_LOSSES:
            until = now + timedelta(hours=COOLDOWN_AFTER_LOSSES_HOURS)
            lab["losses_cooldown_until"] = until.isoformat()
            lab["consecutive_losses"] = 0
            if notifications is not None:
                notifications.append(
                    f"⏸ PRO: {MAX_CONSECUTIVE_LOSSES} убытка подряд. "
                    f"Пауза на {COOLDOWN_AFTER_LOSSES_HOURS}ч"
                )

    if reason in ("stop", "trailing_stop", "liquidation"):
        cds = lab.setdefault("stop_cooldowns", {})
        cds[trade["symbol"]] = (now + timedelta(hours=STOP_COOLDOWN_HOURS)).isoformat()

    lab["active_trade"] = None
    return lab


# ─── Main tick ────────────────────────────────────────────────────────────────

def pro_tick() -> dict[str, Any]:
    exchange = make_pro_exchange()
    lab = load_pro_state()
    lab["last_tick"] = now_iso()
    lab["last_event"] = None
    lab = _roll_day(lab)
    now = datetime.now(timezone.utc)
    notifications: list[str] = []

    if not lab.get("enabled", True):
        save_pro_state(lab)
        return {"status": "disabled", "virtual_usdt": float(lab.get("virtual_usdt") or 0),
                "notifications": notifications}

    active = lab.get("active_trade")

    if active:
        symbol = active["symbol"]
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception as exc:
            save_pro_state(lab)
            return {"status": "error", "error": str(exc), "notifications": notifications}
        if price <= 0:
            save_pro_state(lab)
            return {"status": "holding", "note": "price=0", "notifications": notifications}

        side = active["side"]
        atr = float(active.get("atr") or 0)

        if side == "long":
            active["highest_price"] = max(float(active.get("highest_price") or price), price)
        else:
            active["lowest_price"] = min(float(active.get("lowest_price") or price), price)

        # 1) Liquidation
        liq = float(active.get("liquidation_price") or 0)
        liq_hit = (side == "long" and liq > 0 and price <= liq) or (side == "short" and liq > 0 and price >= liq)
        if liq_hit:
            lab = _close_trade(lab, liq, "liquidation", notifications)
            save_pro_state(lab)
            return _closed_result(lab, "liquidation", symbol, notifications)

        # 2) Stop (trailing once activated, otherwise initial/structure stop)
        effective_stop = float(active.get("trailing_stop") or active.get("stop_price") or 0)
        stop_hit = (side == "long" and price <= effective_stop) or (side == "short" and price >= effective_stop)
        if stop_hit:
            reason = "trailing_stop" if active.get("trailing_activated") else "stop"
            lab = _close_trade(lab, price, reason, notifications)
            save_pro_state(lab)
            return _closed_result(lab, reason, symbol, notifications)

        # 3) Partial take-profit at +2 ATR (once) → stop to breakeven, trailing on
        if not active.get("partial_done") and atr > 0:
            avg = float(active["avg_entry_price"])
            reached = (price >= avg + PARTIAL_TP_ATR * atr) if side == "long" else (price <= avg - PARTIAL_TP_ATR * atr)
            if reached:
                pnl = _apply_partial_tp(lab, active, price)
                lab["active_trade"] = active
                save_pro_state(lab)
                notifications.append(f"🎯 PRO: фиксация 50% на +2 ATR, стоп в безубыток (+${pnl:.2f})")
                return {"status": "partial_tp", "symbol": symbol, "pnl_usdt": round(pnl, 2),
                        "virtual_usdt": float(lab["virtual_usdt"]), "event": lab.get("last_event"),
                        "notifications": notifications}

        # 4) Pyramiding — add into profit only
        if _can_pyramid(active, price):
            _apply_add(active, price)
            lab["active_trade"] = active
            save_pro_state(lab)
            notifications.append(f"➕ PRO: добор по тренду #{active['adds']} @ {price}")
            return {"status": "pyramided", "symbol": symbol, "adds": active["adds"],
                    "avg_entry_price": active["avg_entry_price"],
                    "virtual_usdt": float(lab["virtual_usdt"]), "notifications": notifications}

        # 5) Trail the remainder by 3 ATR once trailing is active
        if active.get("trailing_activated") and atr > 0:
            if side == "long":
                new_trail = float(active["highest_price"]) - TRAIL_ATR * atr
                active["trailing_stop"] = max(new_trail, float(active.get("trailing_stop") or active["stop_price"]))
            else:
                new_trail = float(active["lowest_price"]) + TRAIL_ATR * atr
                active["trailing_stop"] = min(new_trail, float(active.get("trailing_stop") or active["stop_price"]))

        lab["active_trade"] = active
        save_pro_state(lab)
        unreal = _settle_pnl(side, float(active["avg_entry_price"]), price, float(active["position_size_usdt"]))
        return {
            "status": "holding",
            "symbol": symbol,
            "side": side,
            "avg_entry_price": float(active["avg_entry_price"]),
            "current_price": price,
            "stop": effective_stop,
            "adds": int(active.get("adds") or 0),
            "partial_done": bool(active.get("partial_done")),
            "unrealized_pnl_usdt": round(unreal, 2),
            "virtual_usdt": float(lab["virtual_usdt"]),
            "notifications": notifications,
        }

    # No active trade — account-wide circuit breakers first
    gblock = _global_entry_block(lab, now)
    if gblock:
        _log_blocked_entry(lab, "*", "-", gblock)
        if gblock == "daily_loss_limit" and lab.get("daily_limit_notified_date") != _today_utc():
            lab["daily_limit_notified_date"] = _today_utc()
            notifications.append(f"⛔ PRO: дневной лимит убытка (-{DAILY_LOSS_LIMIT_PCT:.0f}%) — входы остановлены")
        save_pro_state(lab)
        return {"status": "blocked", "reason": gblock,
                "day_loss_pct": round(_current_day_loss_pct(lab), 2),
                "virtual_usdt": float(lab.get("virtual_usdt") or 0), "notifications": notifications}

    # Scan top-10 for a trend-aligned entry
    chosen = None
    chosen_funding = None
    blocked: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        try:
            data = _analyze_symbol(exchange, symbol)
        except Exception:
            continue
        if not data:
            continue
        sig = _entry_signal(data)
        if not sig:
            continue
        funding = _funding_pct(exchange, symbol)
        if _funding_blocks(sig["side"], funding):
            reason = "funding_overheated" if sig["side"] == "long" else "funding_negative"
            _log_blocked_entry(lab, symbol, sig["side"], reason)
            blocked.append({"symbol": symbol, "side": sig["side"], "reason": reason})
            continue
        until = _parse_iso((lab.get("stop_cooldowns") or {}).get(symbol))
        if until and now < until:
            _log_blocked_entry(lab, symbol, sig["side"], "stop_cooldown")
            blocked.append({"symbol": symbol, "side": sig["side"], "reason": "stop_cooldown"})
            continue
        chosen = sig
        chosen_funding = funding
        break

    if chosen:
        lab = _open_trade(lab, chosen, chosen_funding)
        save_pro_state(lab)
        return {
            "status": "opened",
            "strategy": "pro_trend",
            "symbol": chosen["symbol"],
            "side": chosen["side"],
            "htf_trend": chosen.get("htf_trend"),
            "entry_price": chosen["entry_price"],
            "stop_price": chosen["stop_price"],
            "funding_pct": chosen_funding,
            "virtual_usdt": float(lab["virtual_usdt"]),
            "event": lab.get("last_event"),
            "notifications": notifications,
        }

    save_pro_state(lab)
    return {"status": "idle", "virtual_usdt": float(lab.get("virtual_usdt") or 0),
            "signals_checked": len(SYMBOLS), "blocked_entries": blocked, "notifications": notifications}


def _closed_result(lab: dict[str, Any], reason: str, symbol: str, notifications: list[str]) -> dict[str, Any]:
    last = lab["journal"][-1] if lab["journal"] else {}
    return {
        "status": "closed",
        "reason": reason,
        "symbol": symbol,
        "pnl_usdt": last.get("pnl_usdt"),
        "virtual_usdt": float(lab["virtual_usdt"]),
        "event": lab.get("last_event"),
        "notifications": notifications,
    }


# ─── Reporting ─────────────────────────────────────────────────────────────────

def _closed_trades(journal: list[dict]) -> list[dict]:
    return [x for x in journal if x.get("action") == "CLOSE"]


def _stats(closed: list[dict]) -> dict[str, Any]:
    wins = [x for x in closed if float(x.get("pnl_usdt") or 0) > 0]
    total_pnl = sum(float(x.get("pnl_usdt") or 0) for x in closed)
    n = len(closed)
    return {
        "count": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "winrate": (len(wins) / n * 100) if n else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": (total_pnl / n) if n else 0.0,
    }


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


def pro_summary() -> str:
    lab = load_pro_state()
    initial = float(lab.get("initial_usdt") or VIRTUAL_USDT_START)
    current = float(lab.get("virtual_usdt") or VIRTUAL_USDT_START)
    growth = (current / initial - 1) * 100 if initial else 0.0
    journal = lab.get("journal") or []
    st = _stats(_closed_trades(journal))

    lines = [
        "🎯 PRO LAB (фьючерсы, виртуальный)",
        "",
        f"Философия: тренд 4h + добор в прибыль + funding-фильтр",
        f"Включён: {'да' if lab.get('enabled', True) else 'нет'}",
        f"ТФ тренда: {HTF_TIMEFRAME} | Вход: {LTF_TIMEFRAME} | Плечо: {LEVERAGE}x | Риск/сделка: {RISK_PER_TRADE_PCT}%",
        f"Монеты: {', '.join(_base_coin(s) for s in SYMBOLS)}",
        f"Депо старт: ${initial:.2f}",
        f"Депо сейчас: ${current:.2f}",
        f"Результат: {growth:+.2f}%",
        "",
        f"Закрытых сделок: {st['count']}",
        f"Плюсовых: {st['wins']} | Минусовых: {st['losses']}",
        f"Винрейт: {st['winrate']:.1f}% | Средний PnL: ${st['avg_pnl']:+.2f}",
        f"Последний тик: {lab.get('last_tick') or 'нет'}",
    ]

    active = lab.get("active_trade")
    if active:
        stop = float(active.get("trailing_stop") or active.get("stop_price") or 0)
        lines += [
            "",
            "Открытая позиция:",
            f"  {active['symbol']} | {active['side'].upper()} [{active['strategy']}]",
            f"  Средний вход: {active.get('avg_entry_price')} | Стоп: {stop:.6g}",
            f"  Доборов: {int(active.get('adds') or 0)}/{int(active.get('max_adds') or MAX_ADDS)} | "
            f"Частичная фиксация: {'да' if active.get('partial_done') else 'нет'}",
            f"  Позиция: ${float(active.get('position_size_usdt') or 0):.2f} | x{LEVERAGE}",
            f"  Трейлинг: {'активен' if active.get('trailing_activated') else 'ждёт'}",
            f"  Открыта: {active.get('opened_at')}",
        ]
    else:
        lines += ["", "Открытая позиция: нет"]

    return "\n".join(lines)


def pro_report(days: int = 7) -> str:
    lab = load_pro_state()
    closed = _filter_closed_by_days(lab.get("journal") or [], days)
    if not closed:
        return f"📊 PRO REPORT ({days}д)\n\nСделок за период нет."

    st = _stats(closed)
    lines = [
        f"📊 PRO REPORT ({days}д)",
        f"Сделок: {st['count']} | Винрейт: {st['winrate']:.1f}% | PnL: ${st['total_pnl']:+.2f} | "
        f"Средний: ${st['avg_pnl']:+.2f}",
        "",
    ]

    sorted_trades = sorted(closed, key=lambda x: float(x.get("pnl_usdt") or 0), reverse=True)
    lines += ["Топ-3 лучших:"]
    for i, t in enumerate(sorted_trades[:3], 1):
        lines.append(
            f"  {i}. {t.get('symbol')} {t.get('side')} доборов:{t.get('adds', 0)} "
            f"${float(t.get('pnl_usdt') or 0):+.2f} | {t.get('exit_reason')}"
        )
    lines += ["", "Топ-3 худших:"]
    for i, t in enumerate(reversed(sorted_trades[-3:]), 1):
        lines.append(
            f"  {i}. {t.get('symbol')} {t.get('side')} доборов:{t.get('adds', 0)} "
            f"${float(t.get('pnl_usdt') or 0):+.2f} | {t.get('exit_reason')}"
        )
    return "\n".join(lines)


def export_pro_csv(days: int = 30) -> Path:
    lab = load_pro_state()
    closed = _filter_closed_by_days(lab.get("journal") or [], days)

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"pro_{days}d_{ts}.csv"

    fieldnames = [
        "strategy", "side", "symbol", "market_regime", "funding_pct",
        "adds", "initial_notional", "position_size_usdt", "leverage",
        "entry_price", "avg_entry_price", "stop_price", "liquidation_price",
        "exit_price", "exit_reason", "partial_pnl_usdt", "pnl_pct", "pnl_usdt",
        "duration_hours", "deposit_before", "deposit_after",
        "opened_at", "closed_at", "er_htf_trend", "er_condition",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in closed:
            er = row.get("entry_reason") or {}
            out = dict(row)
            out["er_htf_trend"] = er.get("htf_trend")
            out["er_condition"] = er.get("condition")
            writer.writerow(out)
    return path


def pro_reset() -> str:
    fresh = default_pro_state()
    update_portfolio(lambda s: s.update({PRO_KEY: fresh}))
    return f"🎯 PRO Lab сброшен. Виртуальный баланс: ${VIRTUAL_USDT_START:.2f} USDT"


def pro_set_enabled(enabled: bool) -> str:
    lab = load_pro_state()
    lab["enabled"] = bool(enabled)
    save_pro_state(lab)
    return "🎯 PRO Lab включён" if enabled else "🎯 PRO Lab выключен"


def format_pro_event(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None
    action = event.get("action")
    symbol = event.get("symbol", "?")

    if action == "OPEN":
        er = event.get("entry_reason") or {}
        funding = event.get("funding_pct")
        funding_line = f"Funding: {funding:+.4f}%\n" if funding is not None else ""
        return (
            "🎯 PRO DEMO ENTRY\n\n"
            f"Пара: {symbol} | {event.get('side', '').upper()}\n"
            f"Тренд 4h: {er.get('htf_trend')}\n"
            f"Вход: {event.get('entry_price')}\n"
            f"Стоп: {event.get('stop_price')}\n"
            f"Лик: {float(event.get('liquidation_price') or 0):.4g}\n"
            f"{funding_line}"
            f"Позиция: ${float(event.get('position_size_usdt') or 0):.2f} | x{LEVERAGE}\n"
            f"Условие: {er.get('condition', '')}\n"
            f"Депо: ${float(event.get('deposit_before') or 0):.2f}"
        )

    if action == "PARTIAL_TP":
        return (
            "🎯 PRO DEMO — частичная фиксация\n\n"
            f"Пара: {symbol} | {event.get('side', '').upper()}\n"
            f"Закрыто {float(event.get('fraction') or 0) * 100:.0f}% на +2 ATR\n"
            f"PnL части: ${float(event.get('pnl_usdt') or 0):+.2f}\n"
            f"Стоп перенесён в безубыток"
        )

    if action == "CLOSE":
        pnl = float(event.get("pnl_usdt") or 0)
        pnl_pct = float(event.get("pnl_pct") or 0)
        return (
            "🎯 PRO DEMO EXIT\n\n"
            f"Пара: {symbol} | {event.get('side', '').upper()}\n"
            f"Причина: {event.get('exit_reason')}\n"
            f"Доборов: {event.get('adds', 0)}\n"
            f"Вход: {event.get('entry_price')} → Выход: {event.get('exit_price')}\n"
            f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}% депо)\n"
            f"Длительность: {event.get('duration_hours')}ч\n"
            f"Депо: ${float(event.get('deposit_before') or 0):.2f} → ${float(event.get('deposit_after') or 0):.2f}"
        )
    return None


# ─── Battle: futures_lab vs pro_lab side by side ────────────────────────────────

def battle_report() -> str:
    """Side-by-side comparison of the two simulators (same start, same rules)."""
    from app.futures_lab import load_futures_state, VIRTUAL_USDT_START as FUT_START

    fut = load_futures_state()
    pro = load_pro_state()

    def block(name: str, lab: dict, start_default: float) -> tuple[list[str], float]:
        initial = float(lab.get("initial_usdt") or start_default)
        current = float(lab.get("virtual_usdt") or start_default)
        growth = (current / initial - 1) * 100 if initial else 0.0
        st = _stats(_closed_trades(lab.get("journal") or []))
        active = lab.get("active_trade")
        active_str = (
            f"{active.get('symbol')} {active.get('side', '').upper()}" if active else "нет"
        )
        lines = [
            f"{name}",
            f"  Депо: ${initial:.2f} → ${current:.2f} ({growth:+.2f}%)",
            f"  Сделок: {st['count']} | WR: {st['winrate']:.1f}% | "
            f"W:{st['wins']}/L:{st['losses']}",
            f"  PnL всего: ${st['total_pnl']:+.2f} | Средний: ${st['avg_pnl']:+.2f}",
            f"  Открыта сейчас: {active_str}",
        ]
        return lines, current

    fut_lines, fut_cur = block("🤖 FUTURES LAB (откаты, мелкий ТФ)", fut, FUT_START)
    pro_lines, pro_cur = block("🎯 PRO LAB (тренд 4h + пирамидинг)", pro, VIRTUAL_USDT_START)

    if fut_cur > pro_cur:
        verdict = f"🏆 Впереди FUTURES LAB (+${fut_cur - pro_cur:.2f})"
    elif pro_cur > fut_cur:
        verdict = f"🏆 Впереди PRO LAB (+${pro_cur - fut_cur:.2f})"
    else:
        verdict = "🤝 Ничья"

    lines = ["⚔️ BATTLE: FUTURES vs PRO", ""]
    lines += fut_lines + [""] + pro_lines + ["", "━━━━━━━━━━━━", verdict]
    return "\n".join(lines)
