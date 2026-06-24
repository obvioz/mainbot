"""Offline unit tests for the futures_lab macro 4h direction filter (no network).

Verifies the strict trend-direction gate: both the BTC barometer and the traded
coin must agree on a 4h trend before any entry, FLAT on either stands aside, and
existing positions keep being managed regardless of regime. Also checks the
hourly cache and the 4h regime classifier. Run: python tests/test_regime_filter.py
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


print("\n[Test 1] BTC UP + coin UP -> long allowed, short blocked")
check("long allowed (UP/UP)", f._direction_filter_block("long", "UP", "UP") is None)
check("short blocked (UP/UP) -> regime_mismatch", f._direction_filter_block("short", "UP", "UP") == "regime_mismatch")


print("\n[Test 2] BTC DOWN + coin DOWN -> short allowed, long blocked")
check("short allowed (DOWN/DOWN)", f._direction_filter_block("short", "DOWN", "DOWN") is None)
check("long blocked (DOWN/DOWN) -> regime_mismatch", f._direction_filter_block("long", "DOWN", "DOWN") == "regime_mismatch")


print("\n[Test 3] BTC UP + coin FLAT -> nothing (divergence)")
check("long blocked (UP/FLAT) -> btc_coin_divergence", f._direction_filter_block("long", "UP", "FLAT") == "btc_coin_divergence")
check("short blocked (UP/FLAT) -> btc_coin_divergence", f._direction_filter_block("short", "UP", "FLAT") == "btc_coin_divergence")
check("allowed_now == ничего (UP/FLAT)", f._allowed_now("UP", "FLAT") == "ничего")


print("\n[Test 4] BTC FLAT -> nothing (sideways), regardless of coin")
for coin in ("UP", "DOWN", "FLAT"):
    check(f"long blocked (FLAT/{coin}) -> flat_market", f._direction_filter_block("long", "FLAT", coin) == "flat_market")
    check(f"short blocked (FLAT/{coin}) -> flat_market", f._direction_filter_block("short", "FLAT", coin) == "flat_market")
check("allowed_now == ничего (FLAT/UP)", f._allowed_now("FLAT", "UP") == "ничего")


print("\n[Test 5] BTC UP + coin DOWN -> nothing (divergence)")
check("long blocked (UP/DOWN) -> btc_coin_divergence", f._direction_filter_block("long", "UP", "DOWN") == "btc_coin_divergence")
check("short blocked (UP/DOWN) -> btc_coin_divergence", f._direction_filter_block("short", "UP", "DOWN") == "btc_coin_divergence")
# Symmetric: BTC DOWN + coin UP
check("long blocked (DOWN/UP) -> btc_coin_divergence", f._direction_filter_block("long", "DOWN", "UP") == "btc_coin_divergence")


print("\n[Test 6] Open position keeps being managed when regime turns FLAT")
lab = fresh_lab()
lab["macro_regime"] = {"btc": "FLAT", "coins": {"BTC/USDT:USDT": "FLAT", "ETH/USDT:USDT": "FLAT"}}
lab["last_regime_update"] = f.now_iso()
lab["active_trade"] = make_trade("long", entry=100.0, deposit=lab["virtual_usdt"])
before = lab["virtual_usdt"]
# Closing path (stop management) must work irrespective of FLAT regime — the
# direction filter is an entry-only gate and is never consulted on close.
f._close_trade(lab, 98.0, "stop", [])
last = lab["journal"][-1]
check("position closed normally under FLAT", last.get("action") == "CLOSE")
check("loss applied to balance (managed, not frozen)", lab["virtual_usdt"] < before)
check("active_trade cleared after close", lab.get("active_trade") is None)


print("\n[Test 7] Hourly cache: regime not recomputed more than once per hour")
class FakeExchange:
    def __init__(self):
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.calls += 1
        # 320 fake 4h candles trending up: close rises each candle
        base = 100.0
        return [[i, base + i, base + i + 1, base + i - 1, base + i + 0.5, 10] for i in range(limit or 320)]

lab = fresh_lab()
ex = FakeExchange()
now = datetime.now(timezone.utc)
r1 = f._get_macro_regimes(ex, lab, now)
calls_after_first = ex.calls
check("first call computes regimes (fetched)", calls_after_first > 0)
check("cache stores last_regime_update", lab.get("last_regime_update") is not None)
# Second call 30 min later -> should use cache, no extra fetches
r2 = f._get_macro_regimes(ex, lab, now + timedelta(minutes=30))
check("no refetch within the hour", ex.calls == calls_after_first)
check("cached regimes returned unchanged", r2 == r1)
# Call just past the hour -> recompute
r3 = f._get_macro_regimes(ex, lab, now + timedelta(hours=1, minutes=1))
check("refetch after >1h", ex.calls > calls_after_first)
# force=True always recomputes
calls_before_force = ex.calls
f._get_macro_regimes(ex, lab, now + timedelta(hours=1, minutes=2), force=True)
check("force=True recomputes", ex.calls > calls_before_force)


print("\n[Test 8] 4h regime classifier (EMA50/EMA200 + slope)")
# UP: price>EMA200, EMA50 rising, EMA50>EMA200
check("UP classified", f._classify_macro_regime(price=110.0, ema50=105.0, ema50_prev=103.0, ema200=100.0) == "UP")
# DOWN: price<EMA200, EMA50 falling, EMA50<EMA200
check("DOWN classified", f._classify_macro_regime(price=90.0, ema50=95.0, ema50_prev=97.0, ema200=100.0) == "DOWN")
# FLAT: price above EMA200 but EMA50 below EMA200 (mixed)
check("mixed -> FLAT", f._classify_macro_regime(price=101.0, ema50=99.0, ema50_prev=98.0, ema200=100.0) == "FLAT")
# FLAT: rising EMA50 but price below EMA200
check("price below EMA200 -> FLAT", f._classify_macro_regime(price=99.0, ema50=105.0, ema50_prev=103.0, ema200=100.0) == "FLAT")
# NaN -> FLAT
check("nan -> FLAT", f._classify_macro_regime(price=float("nan"), ema50=1.0, ema50_prev=1.0, ema200=1.0) == "FLAT")


print("\n[Test 9] Macro filter is additive on top of existing breakers")
# A signal aligned with the 4h trend still respects per-signal breakers (stop cooldown).
lab = fresh_lab()
lab["active_trade"] = make_trade("long", entry=100.0, deposit=lab["virtual_usdt"])
f._close_trade(lab, 98.0, "stop", [])  # sets BTC stop cooldown
now = datetime.now(timezone.utc)
sig = {"side": "long", "symbol": "BTC/USDT:USDT", "strategy": "x", "regime": "uptrend"}
check("direction filter would allow (UP/UP)", f._direction_filter_block("long", "UP", "UP") is None)
check("but stop cooldown still blocks", f._signal_entry_block(lab, sig, now) == "stop_cooldown")


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
