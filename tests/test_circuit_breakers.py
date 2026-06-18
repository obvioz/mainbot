"""Offline unit tests for futures_lab circuit breakers (no network).

Tests the pure decision functions directly so the trend/cooldown/limit logic
is verified without hitting Bybit. Run: python tests/test_circuit_breakers.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.futures_lab as f

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def fresh_lab():
    lab = f.default_futures_state()
    lab["day_start_balance"] = 1000.0
    lab["virtual_usdt"] = 1000.0
    lab["day_start_date"] = f._today_utc()
    return lab


def make_trade(side, entry=100.0, deposit=1000.0):
    return {
        "strategy": "test", "side": side, "symbol": "BTC/USDT:USDT",
        "entry_price": entry, "stop_price": entry * (0.98 if side == "long" else 1.02),
        "position_size_usdt": 500.0, "deposit_before": deposit,
        "take_profit_price": entry, "liquidation_price": entry * (0.8 if side == "long" else 1.2),
        "entry_reason": {}, "market_regime": "range",
        "opened_at": f.now_iso(),
    }


print("\n[Test 1] 3 consecutive losses -> 4th entry blocked, 6h pause")
lab = fresh_lab()
now = datetime.now(timezone.utc)
# Simulate 3 losing closes
for i in range(3):
    lab["active_trade"] = make_trade("long", entry=100.0, deposit=lab["virtual_usdt"])
    # exit below entry => loss
    f._close_trade(lab, 98.0, "stop", [])
check("consecutive_losses reset to 0 after pause triggered", lab["consecutive_losses"] == 0)
check("losses_cooldown_until is set", lab.get("losses_cooldown_until") is not None)
block = f._global_entry_block(lab, now)
check("4th entry globally blocked (consecutive_loss_cooldown)", block == "consecutive_loss_cooldown")
# After cooldown expires
future = now + timedelta(hours=f.COOLDOWN_AFTER_LOSSES_HOURS + 1)
check("entry allowed again after 6h", f._global_entry_block(lab, future) is None)


print("\n[Test 2] Daily loss 6% -> entries stopped until next day")
lab = fresh_lab()
lab["virtual_usdt"] = 940.0  # -6% from 1000
check("day loss pct == 6", abs(f._current_day_loss_pct(lab) - 6.0) < 1e-9)
check("entry blocked at daily limit", f._global_entry_block(lab, now) == "daily_loss_limit")
lab2 = fresh_lab()
lab2["virtual_usdt"] = 945.0  # -5.5%, below limit
check("entry allowed below limit (-5.5%)", f._global_entry_block(lab2, now) is None)
# Day rolls over -> baseline resets, limit clears
lab["day_start_date"] = "2000-01-01"
f._roll_day(lab)
check("day roll resets baseline to current balance", lab["day_start_balance"] == 940.0)
check("day loss pct == 0 after roll", f._current_day_loss_pct(lab) == 0.0)
check("entry allowed after new day", f._global_entry_block(lab, now) is None)


print("\n[Test 3] Downtrend -> long blocked, short allowed")
# price below falling EMA50
regime = f._classify_regime(price=95.0, ema50=100.0, ema50_prev=102.0)
check("regime classified as downtrend", regime == "downtrend")
check("long blocked in downtrend", not f._entry_allowed_by_regime("long", regime))
check("short allowed in downtrend", f._entry_allowed_by_regime("short", regime))
sig_long = {"side": "long", "symbol": "BTC/USDT:USDT", "strategy": "x", "regime": "downtrend"}
sig_short = {"side": "short", "symbol": "BTC/USDT:USDT", "strategy": "x", "regime": "downtrend"}
check("signal long blocked (regime_downtrend)", f._signal_entry_block(fresh_lab(), sig_long, now) == "regime_downtrend")
check("signal short allowed in downtrend", f._signal_entry_block(fresh_lab(), sig_short, now) is None)


print("\n[Test 4] Uptrend -> short blocked, long allowed")
regime = f._classify_regime(price=105.0, ema50=100.0, ema50_prev=98.0)
check("regime classified as uptrend", regime == "uptrend")
check("short blocked in uptrend", not f._entry_allowed_by_regime("short", regime))
check("long allowed in uptrend", f._entry_allowed_by_regime("long", regime))
# range case
regime_r = f._classify_regime(price=101.0, ema50=100.0, ema50_prev=100.0)  # flat slope
check("flat slope -> range", regime_r == "range")
check("both sides allowed in range", f._entry_allowed_by_regime("long", "range") and f._entry_allowed_by_regime("short", "range"))


print("\n[Test 5] Profitable trade after 2 losses -> counter reset to 0")
lab = fresh_lab()
for i in range(2):
    lab["active_trade"] = make_trade("long", entry=100.0, deposit=lab["virtual_usdt"])
    f._close_trade(lab, 98.0, "stop", [])
check("counter == 2 after two losses", lab["consecutive_losses"] == 2)
lab["active_trade"] = make_trade("long", entry=100.0, deposit=lab["virtual_usdt"])
f._close_trade(lab, 105.0, "take", [])  # profit
check("counter reset to 0 after a win", lab["consecutive_losses"] == 0)


print("\n[Test 6] Pump +5% in 1h while short -> emergency exit detected")
# candles: [ts, open, high, low, close, vol]; in-progress candle open=100, current=106 (+6%)
candles = [
    [0, 99, 99.5, 98, 99, 1],
    [1, 99, 100, 98.5, 100, 1],   # last closed
    [2, 100, 100, 100, 100, 1],   # in-progress, open=100
]
pump = f._short_pump_pct(candles, current_price=106.0)
check("pump pct detected ~6%", abs(pump - 6.0) < 1e-6)
check("pump >= PUMP_GUARD_PCT triggers guard", pump >= f.PUMP_GUARD_PCT)
# small move should not trigger
small = f._short_pump_pct(candles, current_price=101.0)
check("1% move does not trigger guard", small < f.PUMP_GUARD_PCT)


print("\n[Test 7] Short stop tighter than long stop")
check("SHORT_STOP_ATR_MULT < ATR_STOP_MULT", f.SHORT_STOP_ATR_MULT < f.ATR_STOP_MULT)
check("_stop_mult short < long", f._stop_mult("short") < f._stop_mult("long"))
# Same entry/ATR: short stop distance smaller
entry, atr = 100.0, 2.0
long_stop_dist = f._stop_mult("long") * atr
short_stop_dist = f._stop_mult("short") * atr
check("short stop distance < long stop distance", short_stop_dist < long_stop_dist)


print("\n[Test 8] Extreme volatility -> new short blocked")
# atr_extreme flag drives the block
sig = {"side": "short", "symbol": "ETH/USDT:USDT", "strategy": "x", "regime": "range", "atr_extreme": True}
check("short blocked under extreme volatility", f._signal_entry_block(fresh_lab(), sig, now) == "extreme_volatility")
sig_long = {"side": "long", "symbol": "ETH/USDT:USDT", "strategy": "x", "regime": "range", "atr_extreme": True}
check("long NOT blocked by volatility guard (shorts only)", f._signal_entry_block(fresh_lab(), sig_long, now) is None)
# atr_extreme computation: latest > 2x avg
check("atr_extreme true when 3.0 > 2*1.0", 3.0 > f.VOL_GUARD_ATR_MULT * 1.0)
check("atr_extreme false when 1.5 < 2*1.0", not (1.5 > f.VOL_GUARD_ATR_MULT * 1.0))


print("\n[Test 9] Stop cooldown after stop-out blocks same symbol for 2h")
lab = fresh_lab()
lab["active_trade"] = make_trade("long", entry=100.0, deposit=lab["virtual_usdt"])
f._close_trade(lab, 98.0, "stop", [])
sig = {"side": "long", "symbol": "BTC/USDT:USDT", "strategy": "x", "regime": "range"}
check("same symbol blocked by stop_cooldown", f._signal_entry_block(lab, sig, now) == "stop_cooldown")
sig_eth = {"side": "long", "symbol": "ETH/USDT:USDT", "strategy": "x", "regime": "range"}
check("different symbol NOT blocked", f._signal_entry_block(lab, sig_eth, now) is None)
later = now + timedelta(hours=f.STOP_COOLDOWN_HOURS + 0.1)
check("same symbol allowed after 2h", f._signal_entry_block(lab, sig, later) is None)


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
exit(1 if FAIL else 0)
