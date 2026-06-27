"""Offline unit tests for spot take-profit notifications (no network).

Verifies BUG-1 (dust positions < $1 produce zero TP/trailing alerts) and
BUG-2 (only ONE take-profit system survives: advisory TP1 +9%; the legacy
"ВРЕМЯ ФИКСАЦИИ" / "СИЛЬНЫЙ ПЛЮС" messages are gone).

Run: python tests/test_tp_notifications.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.monitor as m

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


def scan(positions, items):
    state = {"positions": positions, "alerts": {}}
    return m._scan_positions(state, items)


print("\n[Test 1] Dust position (< $1) in profit -> ZERO notifications")
# 0.000017 AAVE @ $250 = $0.00425, +34% in profit (avg 186.5)
positions = {"AAVE": {"avg_price": 186.5, "qty": 0.000017, "invested_usdt": 0.00317}}
items = [{"coin": "AAVE", "current": 250.0, "atr_pct": 3.0}]
messages, pending = scan(positions, items)
check("MIN_POSITION_VALUE_USDT constant exists and == 1.0", m.MIN_POSITION_VALUE_USDT == 1.0)
check("dust position value is below threshold", 0.000017 * 250.0 < m.MIN_POSITION_VALUE_USDT)
check("no messages for dust position", messages == [])
check("no pending executions for dust position", pending == [])
check("dust position NOT removed from accounting", "AAVE" in positions)


print("\n[Test 2] Real position (> $1) at +10% -> exactly ONE advisory (TP1), not three")
# 1.0 ETH @ $110, avg 100 => +10% (>= TRAILING_TP1_PCT 9%), value $110 > $1
positions = {"ETH": {"avg_price": 100.0, "qty": 1.0, "invested_usdt": 100.0}}
items = [{"coin": "ETH", "current": 110.0, "atr_pct": 2.0}]
messages, pending = scan(positions, items)
check("exactly one pending execution", len(pending) == 1)
check("the single pending execution is TP1 advisory", pending and pending[0]["type"] == "TP1")
check("TP1 targets 40% of the position", pending and abs(pending[0]["share"] - m.TRAILING_TP1_SHARE) < 1e-9)
# Legacy messages must be gone entirely
joined = "\n".join(messages)
check("no legacy 'ВРЕМЯ ФИКСАЦИИ' message", "ВРЕМЯ ФИКСАЦИИ" not in joined)
check("no legacy 'СИЛЬНЫЙ ПЛЮС' message", "СИЛЬНЫЙ ПЛЮС" not in joined)
check("no plain take-profit messages from scan at all", messages == [])


print("\n[Test 3] Real position below +9% -> no take-profit yet")
positions = {"ETH": {"avg_price": 100.0, "qty": 1.0, "invested_usdt": 100.0}}
items = [{"coin": "ETH", "current": 105.0, "atr_pct": 2.0}]  # +5% only
messages, pending = scan(positions, items)
check("no pending execution below TP1 threshold", pending == [])
check("no messages below TP1 threshold", messages == [])


print("\n[Test 4] Advisory cooldown suppresses repeat TP1 (single-notification goal)")
positions = {
    "ETH": {
        "avg_price": 100.0, "qty": 1.0, "invested_usdt": 100.0,
        # cooldown freshly stamped -> still active
        "advisory_cooldowns": {"TP1": m.datetime.now().isoformat(timespec="seconds")},
    }
}
items = [{"coin": "ETH", "current": 110.0, "atr_pct": 2.0}]
messages, pending = scan(positions, items)
check("TP1 suppressed while on cooldown", pending == [])


print("\n[Test 5] Legacy TP flags are no longer consulted")
# Even with old legacy flags set, dust stays silent and real position still emits TP1 once.
positions = {
    "ETH": {
        "avg_price": 100.0, "qty": 1.0, "invested_usdt": 100.0,
        "take_profit_10_sent": True, "take_profit_15_sent": True,
    }
}
items = [{"coin": "ETH", "current": 110.0, "atr_pct": 2.0}]
messages, pending = scan(positions, items)
check("legacy flags do not block new advisory TP1", len(pending) == 1 and pending[0]["type"] == "TP1")


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
exit(1 if FAIL else 0)
