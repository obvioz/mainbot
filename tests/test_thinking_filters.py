"""Offline tests for the futures_lab three-layer "thinking" entry filters.

Covers the pure decision helpers, the orchestrator dispatch (which_filter),
independent flag toggling, and backward compatibility (all flags off).

Reflects the v2 fixes:
  - Structure re-oriented onto the broken level / range for breakouts
    (range_bound_breakout) while pullback/mean-reversion keep opposite-level R/R.
  - Confluence dropped the redundant htf_trend and uses real independent factors
    (setup_quality, volume, vol_normal, rr_to_level, freshness).

Run: python tests/test_thinking_filters.py
"""
import os
import sys

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


def set_flags(mtf, conf, struct):
    f.MTF_FILTER_ON = mtf
    f.CONFLUENCE_FILTER_ON = conf
    f.STRUCTURE_FILTER_ON = struct


def ramp_candles(n, start, step, vol=100.0):
    """Linear OHLCV ramp: step>0 → uptrend, <0 → downtrend, =0 → flat."""
    out = []
    p = start
    for i in range(n):
        o = p
        c = p + step
        h = max(o, c) + 0.5
        low = min(o, c) - 0.5
        out.append([i, o, h, low, c, vol])
        p = c
    return out


class FakeExchange:
    def __init__(self, per_tf):
        self.per_tf = per_tf
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls.append(timeframe)
        return self.per_tf[timeframe][-limit:]


# A signal with a stop 2 pts below entry (risk=2) and a clean setup.
BASE_SIG = {
    "symbol": "BTC/USDT:USDT",
    "side": "long",
    "strategy": "trend_pullback",
    "current": 100.0,
    "atr": 2.0,
    "ema30": 99.0,
    "stop_price": 98.0,
    "high20": 110.0,
    "low20": 90.0,
    "atr_extreme": False,
    "score_components": {"magnitude": 15.0},
}


# ── LAYER 1: MTF ───────────────────────────────────────────────────────────────
print("\n[Test 1] MTF: higher-TF conflict blocks, alignment passes")
check("long 1d=UP/4h=DOWN → mtf_conflict", f._mtf_filter_block("long", "UP", "DOWN") == "mtf_conflict")
check("long 1d=UP/4h=UP → allowed", f._mtf_filter_block("long", "UP", "UP") is None)
check("long 1d=UP/4h=FLAT → allowed (not DOWN)", f._mtf_filter_block("long", "UP", "FLAT") is None)
check("long 1d=DOWN → mtf_conflict", f._mtf_filter_block("long", "DOWN", "UP") == "mtf_conflict")
check("short 1d=DOWN/4h=DOWN → allowed", f._mtf_filter_block("short", "DOWN", "DOWN") is None)
check("short 1d=UP/4h=DOWN → mtf_conflict", f._mtf_filter_block("short", "UP", "DOWN") == "mtf_conflict")


# ── LAYER 2: Confluence (htf_trend removed; real independent factors) ───────────
print("\n[Test 2] Confluence: htf_trend is gone; factors are the 5 real ones")
factors = f._confluence_factors(
    "long", magnitude=15.0, vol_ratio=1.5, atr_extreme=False, rr_to_level=3.0, is_fresh=True,
)
check("no htf_trend key among factors", "htf_trend" not in factors)
check("factor set is exactly the 5 independent ones",
      set(factors) == {"setup_quality", "volume", "vol_normal", "rr_to_level", "freshness"})

print("\n[Test 3] Confluence: <3 real confirmations blocks, >=3 passes")
# Only setup_quality + rr_to_level true → 2 → block
weak = f._confluence_factors(
    "long", magnitude=15.0, vol_ratio=0.4, atr_extreme=True, rr_to_level=3.0, is_fresh=False,
)
r_weak, s_weak, active_weak = f._confluence_block(weak)
check("2 real confirmations → low_confluence", r_weak == "low_confluence" and s_weak == 2)
# Add volume → 3 → passes
ok = f._confluence_factors(
    "long", magnitude=15.0, vol_ratio=1.5, atr_extreme=True, rr_to_level=3.0, is_fresh=False,
)
r_ok, s_ok, _ = f._confluence_block(ok)
check("3 real confirmations → passes", r_ok is None and s_ok == 3)

print("\n[Test 4] Confluence: a trend-aligned breakout no longer scores 3 'for free'")
# Old bug: htf_trend + room_to_extreme + vol_normal = 3 automatically. Now with those
# gone, a weak setup (poor magnitude, no volume, capped R/R, overextended) scores low.
freebie = f._confluence_factors(
    "short", magnitude=0.0, vol_ratio=0.3, atr_extreme=False, rr_to_level=0.5, is_fresh=False,
)
r_free, s_free, active_free = f._confluence_block(freebie)
check("trend-aligned but low-quality → blocked (was 3-for-free before)", r_free == "low_confluence")
check("only vol_normal survives → score 1", s_free == 1 and active_free == ["vol_normal"])


# ── rr_to_level & freshness helpers ────────────────────────────────────────────
print("\n[Test 5] rr_to_nearest_level & freshness helpers")
# long, entry 100, stop 98 (risk 2), resistance 106 → reward 6 → R/R 3.0
check("R/R computed as reward/risk", f._rr_to_nearest_level("long", 100, 98, 106, 90) == 3.0)
# resistance 102 → reward 2 → R/R 1.0 (< 2 → factor false)
check("tight resistance → R/R 1.0", f._rr_to_nearest_level("long", 100, 98, 102, 90) == 1.0)
# open space above (no resistance) → treated as ample (== MIN_RR)
check("open space → R/R == MIN_RR", f._rr_to_nearest_level("long", 100, 98, None, 90) == f.CONFLUENCE_MIN_RR)
check("short R/R uses support below", f._rr_to_nearest_level("short", 100, 102, None, 94) == 3.0)
check("fresh when near EMA30", f._is_fresh_setup(100.0, 99.0, 2.0) is True)
check("overripe when > FRESHNESS_MAX_ATR from EMA30", f._is_fresh_setup(120.0, 99.0, 2.0) is False)


# ── LAYER 3: Structure — breakout re-oriented on range / broken level ──────────
print("\n[Test 6] Structure breakout: narrow range → range_bound_breakout")
# range_atr 1.9 (< 4) → block regardless of level
r, dist = f._structure_block("short", "breakout", 100.0, 2.0, profit_level=None, range_atr=1.9)
check("breakout in <4 ATR range → range_bound_breakout", r == "range_bound_breakout")
# range_atr None (trending, no clean swings) → not range-bound → passes when open space
r, dist = f._structure_block("short", "breakout", 100.0, 2.0, profit_level=None, range_atr=None)
check("breakout with no clean range + open space → passes", r is None)
# wide range 6 ATR, open space ahead → passes
r, dist = f._structure_block("long", "breakout", 100.0, 2.0, profit_level=None, range_atr=6.0)
check("breakout in wide range + room → passes", r is None)

print("\n[Test 7] Structure breakout: capped by next level ahead (<1.5 ATR)")
# wide range but resistance 102 just 1 ATR ahead → capped
r, dist = f._structure_block("long", "breakout", 100.0, 2.0, profit_level=102.0, range_atr=6.0)
check("breakout capped by level <1.5 ATR ahead → poor_structure_rr", r == "poor_structure_rr" and dist == 1.0)
# resistance 104 = 2 ATR ahead → enough room
r, dist = f._structure_block("long", "breakout", 100.0, 2.0, profit_level=104.0, range_atr=6.0)
check("breakout with 2 ATR room ahead → passes", r is None and dist == 2.0)

print("\n[Test 8] Structure pullback/mean_reversion: opposite-level R/R unchanged")
# pullback long, resistance 101 → 0.5 ATR → poor R/R
r, dist = f._structure_block("long", "trend_pullback", 100.0, 2.0, profit_level=101.0, range_atr=1.5)
check("pullback <1 ATR to level → poor_structure_rr (range ignored)", r == "poor_structure_rr" and dist == 0.5)
# pullback long, resistance 105 → 2.5 ATR → fine; tight range must NOT block a pullback
r, dist = f._structure_block("long", "trend_pullback", 100.0, 2.0, profit_level=105.0, range_atr=1.5)
check("pullback in tight range is NOT range-blocked (breakout-only)", r is None and dist == 2.5)
check("no atr → no block", f._structure_block("long", "breakout", 100, 0, None, 1.0) == (None, None))

print("\n[Test 9] _range_atr helper")
check("range_atr = (maxhigh-minlow)/atr", f._range_atr([104, 106], [100, 98], 2.0) == 4.0)
check("range_atr None when no swings", f._range_atr([], [98], 2.0) is None)


# ── swing / nearest / trend helpers (unchanged) ────────────────────────────────
print("\n[Test 10] swing points, nearest levels, trend, volume")
highs = [1, 2, 3, 2, 1, 2, 5, 2, 1]
lows = [9, 8, 7, 8, 9, 8, 5, 8, 9]
sh, sl = f._swing_points(highs, lows, wing=1, lookback=20)
check("swing highs detect local maxima", 3 in sh and 5 in sh)
check("swing lows detect local minima", 7 in sl and 5 in sl)
nr, ns = f._nearest_levels(4.0, [3, 5, 8], [1, 2, 6])
check("nearest resistance above 4 is 5", nr == 5)
check("nearest support below 4 is 2", ns == 2)
check("rising closes → UP", f._trend_from_closes([100 + i * 0.5 for i in range(260)]) == "UP")
check("falling closes → DOWN", f._trend_from_closes([200 - i * 0.5 for i in range(260)]) == "DOWN")
check("volume spike ratio > 1", f._volume_ratio([100.0] * 20 + [200.0]) == 2.0)


# ── Orchestrator dispatch (which_filter) via monkeypatched analysis ────────────
def patch_analysis(**overrides):
    base = {
        "trend_1d": "UP", "trend_1h": "UP", "vol_ratio": 1.5,
        "atr_4h": 2.0, "swing_highs": [130.0], "swing_lows": [70.0],
    }
    base.update(overrides)
    f._thinking_analysis = lambda ex, sym: dict(base)


print("\n[Test 11] orchestrator returns correct which_filter per layer")
set_flags(True, True, True)

patch_analysis(trend_1d="DOWN")   # MTF fires first
reason, which, details, _ = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("MTF block → which_filter=mtf", reason == "mtf_conflict" and which == "mtf")

# MTF passes; confluence fails (weak: no volume, overextended, capped R/R)
patch_analysis(trend_1d="UP", vol_ratio=0.3, swing_highs=[101.0], swing_lows=[70.0])
weak_sig = dict(BASE_SIG, current=100.0, ema30=90.0, score_components={"magnitude": 0.0})
reason, which, details, _ = f._apply_thinking_filters(None, weak_sig, "UP", {})
check("weak setup → which_filter=confluence", reason == "low_confluence" and which == "confluence")

# MTF+confluence pass; structure fails via range_bound_breakout
patch_analysis(trend_1d="DOWN", vol_ratio=1.5, swing_highs=[63.0], swing_lows=[61.0], atr_4h=1.0)
bo_sig = dict(BASE_SIG, side="short", strategy="breakout", current=61.0, ema30=61.5,
              stop_price=62.0, score_components={"magnitude": 20.0})
reason, which, details, assess = f._apply_thinking_filters(None, bo_sig, "DOWN", {})
check("breakout in tight range → which_filter=structure", reason == "range_bound_breakout" and which == "structure")
check("structure details carry range_atr", details.get("range_atr") is not None)

# All layers pass; assessment fully populated
patch_analysis(trend_1d="UP", vol_ratio=1.5, swing_highs=[130.0], swing_lows=[70.0], atr_4h=2.0)
reason, which, details, assess = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("all layers pass → no block", reason is None and which is None)
check("assessment has trends + confluence + range", all(
    k in assess for k in ("trend_1d", "trend_4h", "trend_1h", "confluence_score", "rr_to_level", "range_atr")))


print("\n[Test 12] independent flag toggling")
# Only structure ON: a would-be MTF/confluence block must NOT block
set_flags(False, False, True)
patch_analysis(trend_1d="DOWN", vol_ratio=0.1, swing_highs=[130.0], swing_lows=[70.0])
reason, which, _, _ = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("MTF+confluence off → only structure can block (open space → pass)", reason is None)
# Only MTF ON: weak confluence must NOT block
set_flags(True, False, False)
patch_analysis(trend_1d="UP", vol_ratio=0.1, swing_highs=[101.0], swing_lows=[70.0])
reason, which, _, _ = f._apply_thinking_filters(None, dict(BASE_SIG, score_components={"magnitude": 0.0}), "UP", {})
check("confluence off → weak confluence does not block", reason is None)


print("\n[Test 13] backward compatibility: all flags off → no work, no block")
set_flags(False, False, False)

def _boom(ex, sym):
    raise AssertionError("_thinking_analysis must NOT be called when all flags off")

f._thinking_analysis = _boom
reason, which, details, assess = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("all off → returns no block", reason is None and which is None)
check("all off → no fetch/analysis performed", details == {} and assess == {})


print("\n[Test 14] _log_blocked_entry records which_filter + details")
lab = {"blocked_entries": []}
f._log_blocked_entry(lab, "BTC/USDT:USDT", "short", "breakout", "r", "range_bound_breakout",
                     which_filter="structure", details={"range_atr": 1.9})
row = lab["blocked_entries"][-1]
check("row has which_filter=structure", row.get("which_filter") == "structure")
check("row has details.range_atr", row.get("details", {}).get("range_atr") == 1.9)
f._log_blocked_entry(lab, "BTC/USDT:USDT", "long", "trend_pullback", "r", "stop_cooldown")
check("legacy block has no which_filter key", "which_filter" not in lab["blocked_entries"][-1])


print("\n[Test 15] integration: _thinking_analysis with a fake exchange")
import importlib
importlib.reload(f)
set_flags(True, True, True)
ex = FakeExchange({
    "1d": ramp_candles(320, 100, 0.5),
    "1h": ramp_candles(320, 100, 0.5, vol=100.0),
    "4h": ramp_candles(60, 100, 1.0),
})
ta = f._thinking_analysis(ex, "BTC/USDT:USDT")
check("integration: trend_1d classified UP", ta["trend_1d"] == "UP")
check("integration: atr_4h computed > 0", ta["atr_4h"] > 0)
check("integration: fetched 1d/4h/1h", set(ex.calls) >= {"1d", "4h", "1h"})


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
