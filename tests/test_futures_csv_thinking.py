"""CSV export must include the thinking-layer fields (FIX 1).

Injects a CLOSE journal row carrying a `thinking` dict, runs export_futures_csv,
and asserts every thinking column is present in the header AND populated.

Run: python tests/test_futures_csv_thinking.py
"""
import csv
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


THINKING_COLS = [
    "trend_1d", "trend_4h", "trend_1h",
    "confluence_score", "confluence_factors", "rr_to_level",
    "nearest_resistance", "nearest_support", "distance_to_level_atr", "range_atr",
]

CLOSE_ROW = {
    "action": "CLOSE",
    "strategy": "breakout",
    "side": "short",
    "symbol": "BTC/USDT:USDT",
    "entry_price": 62002.9,
    "exit_price": 62450.0,
    "pnl_usdt": -10.13,
    "pnl_pct": -1.0,
    "opened_at": "2026-07-13T23:59:06+00:00",
    "closed_at": "2026-07-14T00:59:06+00:00",
    "time": "2026-07-14T00:59:06+00:00",
    "score_components": {"trend": 10, "magnitude": 5, "regime": 25, "volatility": 12},
    "entry_reason": {"condition": "breakdown short"},
    "thinking": {
        "trend_1d": "DOWN", "trend_4h": "DOWN", "trend_1h": "FLAT",
        "confluence_score": 2, "confluence_factors": ["vol_normal", "rr_to_level"],
        "rr_to_level": 2.0,
        "nearest_resistance": 64282.2, "nearest_support": None,
        "distance_to_level_atr": None, "range_atr": 1.56,
    },
}


print("\n[CSV] export includes and populates all thinking fields")
# Point export at an isolated journal so we don't touch real state.
orig_load = f.load_futures_state
f.load_futures_state = lambda: {"journal": [CLOSE_ROW]}
# Recent-cutoff safety: _filter_closed_by_days keeps rows within N days; use a huge window.
try:
    path = f.export_futures_csv(days=100000)
finally:
    f.load_futures_state = orig_load

with open(path, newline="", encoding="utf-8") as fh:
    reader = csv.DictReader(fh)
    header = reader.fieldnames or []
    rows = list(reader)

for col in THINKING_COLS:
    check(f"header contains '{col}'", col in header)

check("exactly one data row exported", len(rows) == 1)
r = rows[0]
check("trend_1d value written", r.get("trend_1d") == "DOWN")
check("trend_4h value written", r.get("trend_4h") == "DOWN")
check("trend_1h value written", r.get("trend_1h") == "FLAT")
check("confluence_score value written", r.get("confluence_score") == "2")
check("confluence_factors joined by |", r.get("confluence_factors") == "vol_normal|rr_to_level")
check("rr_to_level value written", r.get("rr_to_level") == "2.0")
check("nearest_resistance value written", r.get("nearest_resistance") == "64282.2")
check("range_atr value written", r.get("range_atr") == "1.56")
# None values serialize to empty string (not missing columns)
check("nearest_support None → empty cell", r.get("nearest_support") == "")

# Cleanup the generated report file.
try:
    os.remove(path)
except OSError:
    pass


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
