"""Offline tests for the futures_lab three-layer "thinking" entry filters.

Covers the pure decision helpers, the orchestrator dispatch (which_filter),
independent flag toggling, and backward compatibility (all flags off).

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


BASE_SIG = {
    "symbol": "BTC/USDT:USDT",
    "side": "long",
    "current": 100.0,
    "atr": 2.0,
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


# ── LAYER 2: Confluence ────────────────────────────────────────────────────────
print("\n[Test 2] Confluence: <3 factors blocks, >=3 passes")
# 2 factors true (htf_trend + setup_quality); volume/room/vol_normal false
factors_2 = f._confluence_factors(
    "long", magnitude=15.0, vol_ratio=0.5, current=100.0, atr=2.0,
    high20=100.5, low20=90.0, trend_4h="UP", atr_extreme=True,
)
reason2, score2, active2 = f._confluence_block(factors_2)
check("2 confirmations → low_confluence block", reason2 == "low_confluence" and score2 == 2)
# flip volume to true → 3 factors
factors_3 = f._confluence_factors(
    "long", magnitude=15.0, vol_ratio=1.5, current=100.0, atr=2.0,
    high20=100.5, low20=90.0, trend_4h="UP", atr_extreme=True,
)
reason3, score3, active3 = f._confluence_block(factors_3)
check("3 confirmations → passes", reason3 is None and score3 == 3)
check("room_to_extreme false when just under high20", factors_2["room_to_extreme"] is False)
check("vol_normal false when atr_extreme", factors_2["vol_normal"] is False)


# ── LAYER 3: Structure ─────────────────────────────────────────────────────────
print("\n[Test 3] Structure: entry under resistance (<1 ATR) blocks, room passes")
r_block, dist_block = f._structure_block("long", 100.0, 2.0, nearest_resistance=101.0, nearest_support=90.0)
check("long <1 ATR under resistance → poor_structure_rr", r_block == "poor_structure_rr" and dist_block == 0.5)
r_ok, dist_ok = f._structure_block("long", 100.0, 2.0, nearest_resistance=105.0, nearest_support=90.0)
check("long with 2.5 ATR room → passes", r_ok is None and dist_ok == 2.5)
rs_block, _ = f._structure_block("short", 100.0, 2.0, nearest_resistance=110.0, nearest_support=99.0)
check("short <1 ATR over support → poor_structure_rr", rs_block == "poor_structure_rr")
rs_ok, _ = f._structure_block("short", 100.0, 2.0, nearest_resistance=110.0, nearest_support=95.0)
check("short with room below → passes", rs_ok is None)
check("no resistance above → long allowed", f._structure_block("long", 100.0, 2.0, None, 90.0)[0] is None)


print("\n[Test 4] swing points & nearest levels")
highs = [1, 2, 3, 2, 1, 2, 5, 2, 1]
lows = [9, 8, 7, 8, 9, 8, 5, 8, 9]
sh, sl = f._swing_points(highs, lows, wing=1, lookback=20)
check("swing highs detect local maxima (3 and 5)", 3 in sh and 5 in sh)
check("swing lows detect local minima (7 and 5)", 7 in sl and 5 in sl)
nr, ns = f._nearest_levels(4.0, [3, 5, 8], [1, 2, 6])
check("nearest resistance above 4 is 5", nr == 5)
check("nearest support below 4 is 2", ns == 2)


print("\n[Test 5] trend_from_closes & volume_ratio")
check("rising closes → UP", f._trend_from_closes([100 + i * 0.5 for i in range(260)]) == "UP")
check("falling closes → DOWN", f._trend_from_closes([200 - i * 0.5 for i in range(260)]) == "DOWN")
check("flat closes → FLAT", f._trend_from_closes([100.0] * 260) == "FLAT")
check("too few closes → FLAT", f._trend_from_closes([100, 101, 102]) == "FLAT")
check("volume spike ratio > 1", f._volume_ratio([100.0] * 20 + [200.0]) == 2.0)
check("volume below baseline ratio < 1", f._volume_ratio([100.0] * 20 + [50.0]) == 0.5)


# ── Orchestrator dispatch (which_filter) via monkeypatched analysis ─────────────
def patch_analysis(**overrides):
    base = {
        "trend_1d": "UP", "trend_1h": "UP", "vol_ratio": 1.5,
        "atr_4h": 2.0, "swing_highs": [110.0], "swing_lows": [90.0],
    }
    base.update(overrides)
    f._thinking_analysis = lambda ex, sym: dict(base)


print("\n[Test 6] orchestrator returns correct which_filter per layer")
set_flags(True, True, True)

patch_analysis(trend_1d="DOWN")   # MTF should fire first
reason, which, details, _ = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("MTF block → which_filter=mtf", reason == "mtf_conflict" and which == "mtf")

# MTF passes (trend_1d=UP, trend_4h=UP), confluence fails (only 2 factors:
# htf_trend + setup_quality; volume/room/vol_normal false)
patch_analysis(trend_1d="UP", vol_ratio=0.5)
sig_conf = dict(BASE_SIG, high20=100.5, atr_extreme=True)  # room false, vol_normal false
reason, which, details, _ = f._apply_thinking_filters(None, sig_conf, "UP", {})
check("confluence block → which_filter=confluence", reason == "low_confluence" and which == "confluence")
check("confluence details carry score", details.get("confluence_score") == 2)

# MTF + confluence pass, structure fails (resistance within 1 ATR)
patch_analysis(trend_1d="UP", vol_ratio=1.5, swing_highs=[101.0])
reason, which, details, _ = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("structure block → which_filter=structure", reason == "poor_structure_rr" and which == "structure")
check("structure details carry distance", details.get("distance_to_level_atr") == 0.5)

# All layers pass
patch_analysis(trend_1d="UP", vol_ratio=1.5, swing_highs=[130.0])
reason, which, details, assess = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("all layers pass → no block", reason is None and which is None)
check("assessment records all three trends", assess.get("trend_1d") == "UP" and assess.get("trend_4h") == "UP")
check("assessment records confluence_score", assess.get("confluence_score") is not None)
check("assessment records structure levels", "nearest_resistance" in assess)


print("\n[Test 7] independent flag toggling")
# Only structure ON: a would-be MTF block (trend_1d=DOWN) must NOT block
set_flags(False, False, True)
patch_analysis(trend_1d="DOWN", swing_highs=[130.0])
reason, which, _, _ = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("MTF off → 1d=DOWN does not block", reason is None)
# Only MTF ON: confluence would fail but must NOT block
set_flags(True, False, False)
patch_analysis(trend_1d="UP", vol_ratio=0.1)
reason, which, _, _ = f._apply_thinking_filters(None, dict(BASE_SIG, atr_extreme=True, high20=100.4), "FLAT", {})
check("confluence off → weak confluence does not block", reason is None)


print("\n[Test 8] backward compatibility: all flags off → no work, no block")
set_flags(False, False, False)

def _boom(ex, sym):
    raise AssertionError("_thinking_analysis must NOT be called when all flags off")

f._thinking_analysis = _boom
reason, which, details, assess = f._apply_thinking_filters(None, dict(BASE_SIG), "UP", {})
check("all off → returns no block", reason is None and which is None)
check("all off → no fetch/analysis performed", details == {} and assess == {})


print("\n[Test 9] _log_blocked_entry records which_filter + details")
lab = {"blocked_entries": []}
f._log_blocked_entry(lab, "BTC/USDT:USDT", "long", "trend_pullback", "r", "low_confluence",
                     which_filter="confluence", details={"confluence_score": 2})
row = lab["blocked_entries"][-1]
check("row has which_filter", row.get("which_filter") == "confluence")
check("row has details", row.get("details", {}).get("confluence_score") == 2)
# legacy call without which_filter stays clean
f._log_blocked_entry(lab, "BTC/USDT:USDT", "long", "trend_pullback", "r", "stop_cooldown")
legacy = lab["blocked_entries"][-1]
check("legacy block has no which_filter key", "which_filter" not in legacy)


print("\n[Test 10] _thinking_analysis integration with a fake exchange")
set_flags(True, True, True)
# rebind the real function (Test 8 replaced it)
import importlib
importlib.reload(f)
set_flags(True, True, True)
ex = FakeExchange({
    "1d": ramp_candles(320, 100, 0.5),      # uptrend
    "1h": ramp_candles(320, 100, 0.5, vol=100.0),
    "4h": ramp_candles(60, 100, 1.0),
})
ta = f._thinking_analysis(ex, "BTC/USDT:USDT")
check("integration: trend_1d classified UP", ta["trend_1d"] == "UP")
check("integration: atr_4h computed > 0", ta["atr_4h"] > 0)
check("integration: fetched 1d/4h/1h timeframes", set(ex.calls) >= {"1d", "4h", "1h"})


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
