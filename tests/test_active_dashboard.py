"""Offline unit tests for the unified /active dashboard (no network).

Verifies build_active_trades_report() collects from all four systems (spot,
futures_lab, pro_lab, rotation_lab) and renders both empty and filled sections
with a correct total count. All sources are monkeypatched — no Bybit, no files.

Run: python tests/test_active_dashboard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.bot as b

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


def patch_sources(spot, fut, pro, rot):
    b.get_open_positions = lambda: spot
    b.load_futures_state = lambda: fut
    b.load_pro_state = lambda: pro
    b.load_rotation_state = lambda: rot


_orig = (b.get_open_positions, b.load_futures_state, b.load_pro_state, b.load_rotation_state)


print("\n[Test 1] All systems empty -> every section shows 'нет', total 0")
patch_sources({}, {"active_trade": None}, {"active_trade": None}, {"active_trade": None})
rep = b.build_active_trades_report({})
check("spot section header", "💰 СПОТ" in rep)
check("futures section header", "🤖 ФЬЮЧЕРСЫ (наш бот" in rep)
check("pro section header", "🎯 ФЬЮЧЕРСЫ ПРО" in rep)
check("rotation section header", "🔄 КРИПТОПАРЫ" in rep)
check("spot empty line", "нет открытых позиций" in rep)
check("three 'нет активной сделки' (fut/pro/rot)", rep.count("нет активной сделки") == 3)
check("total is 0", "Всего открытых позиций по всем системам: 0" in rep)


print("\n[Test 2] All systems filled -> total 4, prices/PnL rendered")
patch_sources(
    spot={"BTC": {"avg_price": 60000.0, "qty": 0.01, "entry_count": 2, "dca_levels_used": [7.0]}},
    fut={"active_trade": {"symbol": "ETH/USDT:USDT", "side": "long", "entry_price": 2000.0,
                          "trailing_stop": 1950.0, "strategy": "mean_reversion", "score": 78}},
    pro={"active_trade": {"symbol": "SOL/USDT:USDT", "side": "long", "avg_entry_price": 150.0,
                          "trailing_stop": 145.0, "adds": 1}},
    rot={"active_trade": {"pair": "SOL/BTC", "entry_price": 0.0025, "trailing_activated": True}},
)
price_map = {"BTC": 62000.0, "ETH": 2100.0, "SOL": 160.0}
rep = b.build_active_trades_report(price_map)
check("total is 4", "Всего открытых позиций по всем системам: 4" in rep)
check("spot BTC shown with tranche 2/3", "BTC" in rep and "транш 2/3" in rep)
check("spot BTC PnL positive (62k vs 60k)", "+3.3%" in rep)
check("futures ETH long shown", "ETH/USDT:USDT" in rep and "score 78" in rep)
check("futures ETH PnL +5% (2100 vs 2000)", "+5.0%" in rep)
check("pro SOL with adds count", "SOL/USDT:USDT" in rep and "доборов 1" in rep)
check("rotation pair with trailing active", "SOL/BTC" in rep and "трейлинг активен" in rep)


print("\n[Test 3] Mixed: only spot + pro filled -> total 2")
patch_sources(
    spot={"ETH": {"avg_price": 2000.0, "qty": 0.5, "entry_count": 1}},
    fut={"active_trade": None},
    pro={"active_trade": {"symbol": "BTC/USDT:USDT", "side": "short", "avg_entry_price": 60000.0,
                          "trailing_stop": 61000.0, "adds": 0}},
    rot={"active_trade": None},
)
rep = b.build_active_trades_report({"ETH": 1900.0, "BTC": 59000.0})
check("total is 2", "Всего открытых позиций по всем системам: 2" in rep)
check("two empty sections (fut + rot)", rep.count("нет активной сделки") == 2)
check("spot ETH in drawdown shows negative PnL", "-5.0%" in rep)
check("pro short in profit (60k->59k) shows positive PnL", "+1.7%" in rep)


print("\n[Test 4] Missing price degrades gracefully")
patch_sources(
    spot={},
    fut={"active_trade": {"symbol": "DOGE/USDT:USDT", "side": "long", "entry_price": 0.1,
                          "trailing_stop": 0.09, "strategy": "pro_trend"}},
    pro={"active_trade": None},
    rot={"active_trade": None},
)
rep = b.build_active_trades_report({})  # no DOGE price
check("missing price -> PnL н/д", "PnL н/д" in rep)
check("missing price -> сейчас —", "сейчас —" in rep)
check("still counts the position (total 1)", "Всего открытых позиций по всем системам: 1" in rep)


(b.get_open_positions, b.load_futures_state, b.load_pro_state, b.load_rotation_state) = _orig
print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
