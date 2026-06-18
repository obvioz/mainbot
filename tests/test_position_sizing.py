"""Offline unit tests for score-based position sizing + portfolio risk ceiling.

No network. Run: python tests/test_position_sizing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.futures_lab as f

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {extra}")


def fresh_lab(deposit=1000.0):
    lab = f.default_futures_state()
    lab["virtual_usdt"] = deposit
    lab["day_start_balance"] = deposit
    return lab


print("\n[Test A] get_risk_pct tiers")
check("score 90 -> 3%", f.get_risk_pct(90) == 3.0)
check("score 85 -> 3% (boundary)", f.get_risk_pct(85) == 3.0)
check("score 84 -> 2%", f.get_risk_pct(84) == 2.0)
check("score 78 -> 2%", f.get_risk_pct(78) == 2.0)
check("score 75 -> 2% (boundary)", f.get_risk_pct(75) == 2.0)
check("score 74 -> 1%", f.get_risk_pct(74) == 1.0)
check("score 72 -> 1%", f.get_risk_pct(72) == 1.0)
check("score 70 -> 1% (boundary)", f.get_risk_pct(70) == 1.0)
check("score 69 -> 0 (below MIN_ENTRY_SCORE)", f.get_risk_pct(69) == 0.0)
check("score None -> 0", f.get_risk_pct(None) == 0.0)


print("\n[Test B] position size & risk math are consistent")
# deposit 1000, entry 100, stop 98 => 2% stop distance; risk 3% => notional 1500
size = f._calc_position_size(1000.0, 100.0, 98.0, 3.0)
check("notional size for 3% risk == 1500", abs(size - 1500.0) < 1e-6, f"got {size}")
trade = {"entry_price": 100.0, "stop_price": 98.0, "position_size_usdt": size}
risk = f._position_risk_pct(trade, 1000.0)
check("position risk back-computes to 3%", abs(risk - 3.0) < 1e-6, f"got {risk}")


print("\n[Test C] _resolve_entry_risk — the 5 ТЗ scenarios")
# score 90, empty portfolio -> 3% full
actual, assigned, trimmed, blk = f._resolve_entry_risk(90, 0.0)
check("score 90 empty -> 3% full, not trimmed, not blocked",
      actual == 3.0 and assigned == 3.0 and not trimmed and blk is None,
      f"got {actual,assigned,trimmed,blk}")
# score 78 -> 2%
actual, assigned, trimmed, blk = f._resolve_entry_risk(78, 0.0)
check("score 78 -> 2%", actual == 2.0 and assigned == 2.0 and not trimmed, f"got {actual,assigned,trimmed}")
# score 72 -> 1%
actual, assigned, trimmed, blk = f._resolve_entry_risk(72, 0.0)
check("score 72 -> 1%", actual == 1.0 and assigned == 1.0 and not trimmed, f"got {actual,assigned,trimmed}")
# portfolio at 5%, score 90 wants 3% -> trimmed to remaining 1%
actual, assigned, trimmed, blk = f._resolve_entry_risk(90, 5.0)
check("portfolio 5% + score 90 -> trimmed to 1%, was_trimmed=True",
      abs(actual - 1.0) < 1e-9 and assigned == 3.0 and trimmed and blk is None,
      f"got {actual,assigned,trimmed,blk}")
# portfolio at 5.8%, remaining 0.2% < MIN_RISK_PCT 0.5% -> blocked
actual, assigned, trimmed, blk = f._resolve_entry_risk(90, 5.8)
check("portfolio 5.8% -> blocked (portfolio_risk_ceiling)",
      blk == "portfolio_risk_ceiling" and actual == 0.0,
      f"got {actual,assigned,trimmed,blk}")
# exactly at MIN boundary: remaining == 0.5 -> trim allowed
actual, assigned, trimmed, blk = f._resolve_entry_risk(90, 5.5)
check("portfolio 5.5% -> trimmed to exactly 0.5% (boundary OK)",
      abs(actual - 0.5) < 1e-9 and trimmed and blk is None, f"got {actual,assigned,trimmed,blk}")


print("\n[Test D] _current_portfolio_risk_pct sums open positions")
lab = fresh_lab(1000.0)
lab["active_trade"] = {"entry_price": 100.0, "stop_price": 98.0, "position_size_usdt": 1500.0}
check("active_trade contributes 3% portfolio risk", abs(f._current_portfolio_risk_pct(lab) - 3.0) < 1e-6,
      f"got {f._current_portfolio_risk_pct(lab)}")
# generalized open_positions list (future MAX_CONCURRENT>1)
lab["open_positions"] = [{"entry_price": 50.0, "stop_price": 49.0, "position_size_usdt": 1000.0}]  # 2% risk
check("sum across active + extra positions == 5%", abs(f._current_portfolio_risk_pct(lab) - 5.0) < 1e-6,
      f"got {f._current_portfolio_risk_pct(lab)}")


print("\n[Test E] _open_trade records risk + score journal fields")
lab = fresh_lab(1000.0)
signal = {
    "current": 100.0, "stop_price": 98.0, "side": "long", "symbol": "BTC/USDT:USDT",
    "strategy": "trend_pullback", "atr": 2.0, "regime": "uptrend", "score": 90,
    "score_components": {"trend": 30, "magnitude": 20, "regime": 25, "volatility": 15},
    "entry_reason": {"condition": "x"},
}
f._open_trade(lab, signal, risk_pct=3.0, assigned_risk_pct=3.0, was_trimmed=False, portfolio_risk_before=0.0)
t = lab["active_trade"]
check("position_size == 1500 for 3% full", abs(t["position_size_usdt"] - 1500.0) < 1e-6, f"got {t['position_size_usdt']}")
check("trade.score == 90", t["score"] == 90)
check("trade.actual_risk_pct == 3", t["actual_risk_pct"] == 3.0)
check("trade.was_trimmed False", t["was_trimmed"] is False)
open_row = [r for r in lab["journal"] if r["action"] == "OPEN"][-1]
check("journal OPEN has score_components", open_row.get("score_components", {}).get("trend") == 30)
check("journal OPEN has assigned_risk_pct", open_row.get("assigned_risk_pct") == 3.0)
check("journal OPEN has portfolio_risk_before", open_row.get("portfolio_risk_before") == 0.0)

# trimmed open
lab2 = fresh_lab(1000.0)
f._open_trade(lab2, signal, risk_pct=1.0, assigned_risk_pct=3.0, was_trimmed=True, portfolio_risk_before=5.0)
t2 = lab2["active_trade"]
check("trimmed: position_size == 500 (1% risk)", abs(t2["position_size_usdt"] - 500.0) < 1e-6, f"got {t2['position_size_usdt']}")
check("trimmed: assigned 3 / actual 1 / was_trimmed True",
      t2["assigned_risk_pct"] == 3.0 and t2["actual_risk_pct"] == 1.0 and t2["was_trimmed"] is True)
check("trimmed: portfolio_risk_before == 5.0", t2["portfolio_risk_before"] == 5.0)


print("\n[Test F] per-strategy scoring is strategy-specific with 4 components")
COMP_KEYS = {"trend", "magnitude", "regime", "volatility"}

def comps_ok(score, comps):
    return set(comps.keys()) == COMP_KEYS and abs(sum(comps.values()) - score) < 0.6

# trend_pullback strong (aligned uptrend, decent pullback, calm vol) -> >= 70
data = {"ema9": 105.0, "ema30": 100.0, "atr": 2.0, "current": 103.5, "atr_avg": 2.0,
        "regime": "uptrend", "high20": 110.0, "low20": 90.0}
s, c = f._score_trend_pullback(data, "long")
check("trend_pullback strong scores >= 70", s >= 70, f"got {s} {c}")
check("trend_pullback components sum==score & keys ok", comps_ok(s, c), f"got {s} {c}")
check("trend_pullback aligned regime -> 25", c["regime"] == 25.0, f"got {c}")

# trend_pullback weak (range, tiny gap, high vol) -> < 70 (gets filtered)
weak = {"ema9": 100.5, "ema30": 100.0, "atr": 2.0, "current": 100.4, "atr_avg": 1.0,
        "regime": "range", "high20": 110.0, "low20": 90.0}
sw, cw = f._score_trend_pullback(weak, "long")
check("trend_pullback weak scores < 70 (filtered)", sw < 70, f"got {sw} {cw}")
check("weak high-vol -> volatility component 0", cw["volatility"] == 0.0, f"got {cw}")

# mean_reversion: big deviation, flat trend, range -> high; magnitude is the heavy component
mr = {"ema9": 100.0, "ema30": 100.0, "atr": 2.0, "current": 108.0, "atr_avg": 2.0,
      "regime": "range", "high20": 110.0, "low20": 90.0}
sm, cm = f._score_mean_reversion(mr, "short")
check("mean_reversion big deviation scores >= 70", sm >= 70, f"got {sm} {cm}")
check("mean_reversion magnitude is dominant (>=30)", cm["magnitude"] >= 30, f"got {cm}")
check("mean_reversion components ok", comps_ok(sm, cm), f"got {sm} {cm}")

# breakout: decisive break + EMA aligned -> high; magnitude reflects break distance
bo = {"ema9": 105.0, "ema30": 100.0, "atr": 2.0, "current": 103.0, "atr_avg": 2.0,
      "regime": "uptrend", "high20": 101.0, "low20": 90.0}
sb, cb = f._score_breakout(bo, "long")
check("breakout decisive scores >= 70", sb >= 70, f"got {sb} {cb}")
check("breakout components ok", comps_ok(sb, cb), f"got {sb} {cb}")
# distinctness: same data, the three scorers do NOT all return the same score
scores = {f._score_trend_pullback(data, "long")[0],
          f._score_mean_reversion(data, "long")[0],
          f._score_breakout(data, "long")[0]}
check("three strategies score the same setup differently", len(scores) > 1, f"got {scores}")


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
exit(1 if FAIL else 0)
