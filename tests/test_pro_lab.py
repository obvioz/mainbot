"""Offline unit tests for pro_lab (no network).

Verifies the PRO simulator's core philosophy without hitting Bybit:
  - entry ONLY in the direction of the 4h trend (against-trend blocked)
  - pyramiding adds ONLY into profit, never into a loser
  - funding filter blocks new longs when funding is hot, new shorts when negative
  - partial take-profit at +2 ATR moves the stop to breakeven
  - battle report renders both simulators

Run: python tests/test_pro_lab.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.pro_lab as p

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


def long_data(htf_trend="up", price=100.0, ema9=101.0, ema30=99.0, atr=2.0):
    return {
        "symbol": "BTC/USDT:USDT", "htf_trend": htf_trend, "price": price,
        "ema9": ema9, "ema30": ema30, "atr": atr,
        "ema_fast_4h": 100.0, "ema_slow_4h": 90.0,
    }


def short_data(htf_trend="down", price=100.0, ema9=99.0, ema30=101.0, atr=2.0):
    return {
        "symbol": "ETH/USDT:USDT", "htf_trend": htf_trend, "price": price,
        "ema9": ema9, "ema30": ema30, "atr": atr,
        "ema_fast_4h": 90.0, "ema_slow_4h": 100.0,
    }


print("\n[Test 1] 4h trend classification")
check("up when fast>slow and price>fast", p._classify_htf_trend(100, 95, 90) == "up")
check("down when fast<slow and price<fast", p._classify_htf_trend(80, 90, 95) == "down")
check("none when mixed", p._classify_htf_trend(92, 95, 90) == "none")
check("none on nan", p._classify_htf_trend(float("nan"), 95, 90) == "none")


print("\n[Test 2] Entry only in direction of 4h trend")
sig = p._entry_signal(long_data(htf_trend="up"))
check("long signal in 4h uptrend pullback", sig is not None and sig["side"] == "long")
check("long stop below entry", sig is not None and sig["stop_price"] < sig["entry_price"])
# Same bullish 1h structure but 4h trend is DOWN -> must be blocked (no long against trend)
check("against-trend (4h down) blocks long setup",
      p._entry_signal(long_data(htf_trend="down")) is None)
# 4h trend 'none' -> no entry at all
check("no entry when 4h trend is none",
      p._entry_signal(long_data(htf_trend="none")) is None)
sig_s = p._entry_signal(short_data(htf_trend="down"))
check("short signal in 4h downtrend bounce", sig_s is not None and sig_s["side"] == "short")
check("short stop above entry", sig_s is not None and sig_s["stop_price"] > sig_s["entry_price"])
check("against-trend (4h up) blocks short setup",
      p._entry_signal(short_data(htf_trend="up")) is None)


print("\n[Test 3] Funding sentiment filter")
check("hot funding blocks new LONG", p._funding_blocks("long", 0.10) is True)
check("neutral funding allows LONG", p._funding_blocks("long", 0.02) is False)
check("negative funding blocks new SHORT", p._funding_blocks("short", -0.05) is True)
check("neutral funding allows SHORT", p._funding_blocks("short", 0.01) is False)
check("unknown funding never blocks", p._funding_blocks("long", None) is False)


print("\n[Test 4] Pyramiding adds ONLY into profit")
base = {"side": "long", "avg_entry_price": 100.0, "last_add_price": 100.0,
        "atr": 2.0, "adds": 0, "max_adds": 2, "partial_done": False}
check("add when +1 ATR in profit", p._can_pyramid(dict(base), price=102.0) is True)
check("no add when advance < 1 ATR", p._can_pyramid(dict(base), price=101.0) is False)
check("NEVER add to a loser", p._can_pyramid(dict(base), price=98.0) is False)
done = dict(base); done["partial_done"] = True
check("no add after partial take-profit", p._can_pyramid(done, price=104.0) is False)
maxed = dict(base); maxed["adds"] = 2
check("no add beyond max_adds", p._can_pyramid(maxed, price=104.0) is False)


print("\n[Test 5] Pyramid add re-averages and tightens the stop")
trade = {"side": "long", "avg_entry_price": 100.0, "entry_price": 100.0,
         "position_size_usdt": 500.0, "initial_notional": 500.0,
         "trailing_stop": 96.0, "stop_price": 96.0, "atr": 2.0, "adds": 0}
p._apply_add(trade, price=102.0)
check("adds incremented", trade["adds"] == 1)
check("position size grew by 50% of initial", abs(trade["position_size_usdt"] - 750.0) < 1e-6)
check("avg entry between 100 and 102", 100.0 < trade["avg_entry_price"] < 102.0)
check("stop tightened upward (never loosened)", trade["trailing_stop"] > 96.0)


print("\n[Test 6] Partial take-profit at +2 ATR -> 50% off + stop to breakeven")
lab = {"virtual_usdt": 1000.0, "journal": []}
ptrade = {"side": "long", "avg_entry_price": 100.0, "position_size_usdt": 500.0,
          "trailing_stop": 96.0, "stop_price": 96.0, "strategy": "pro_trend",
          "symbol": "BTC/USDT:USDT"}
pnl = p._apply_partial_tp(lab, ptrade, price=104.0)  # +2 ATR (atr=2 -> +4)
check("realized positive partial pnl", pnl > 0)
check("deposit credited with partial pnl", abs(lab["virtual_usdt"] - (1000.0 + pnl)) < 1e-6)
check("half the position closed", abs(ptrade["position_size_usdt"] - 250.0) < 1e-6)
check("partial_done flag set", ptrade["partial_done"] is True)
check("trailing activated", ptrade["trailing_activated"] is True)
check("stop moved to breakeven (avg entry)", abs(ptrade["stop_price"] - 100.0) < 1e-6)
check("partial logged in journal", any(r.get("action") == "PARTIAL_TP" for r in lab["journal"]))


print("\n[Test 7] _settle_pnl sign/magnitude")
check("long profit", abs(p._settle_pnl("long", 100.0, 110.0, 1000.0) - 100.0) < 1e-6)
check("long loss", p._settle_pnl("long", 100.0, 90.0, 1000.0) < 0)
check("short profit on price drop", p._settle_pnl("short", 100.0, 90.0, 1000.0) > 0)


print("\n[Test 8] pro_tick integration (mocked market) — trend & funding gates")


def run_tick_with(htf_trend, funding):
    """Run pro_tick against a single mocked symbol with given 4h trend + funding."""
    state = {"holder": p.default_pro_state()}
    orig = (p._analyze_symbol, p._funding_pct, p.make_pro_exchange,
            p.load_pro_state, p.save_pro_state)

    def fake_analyze(exchange, symbol):
        return long_data(htf_trend=htf_trend) if symbol == "BTC/USDT:USDT" else None

    p._analyze_symbol = fake_analyze
    p._funding_pct = lambda exchange, symbol: funding
    p.make_pro_exchange = lambda: object()
    p.load_pro_state = lambda: state["holder"]
    p.save_pro_state = lambda lab: state.update({"holder": lab})
    try:
        res = p.pro_tick()
    finally:
        (p._analyze_symbol, p._funding_pct, p.make_pro_exchange,
         p.load_pro_state, p.save_pro_state) = orig
    return res, state["holder"]


res_up, lab_up = run_tick_with("up", funding=0.01)
check("opens a LONG in 4h uptrend with calm funding", res_up["status"] == "opened" and res_up["side"] == "long")
check("active trade recorded after open", lab_up.get("active_trade") is not None)

res_down, lab_down = run_tick_with("down", funding=0.01)
check("no long opened against 4h downtrend (idle)", res_down["status"] == "idle")
check("no active trade after against-trend tick", lab_down.get("active_trade") is None)

res_hot, lab_hot = run_tick_with("up", funding=0.10)
check("hot funding blocks the long entry (idle)", res_hot["status"] == "idle")
check("funding block logged in blocked_entries",
      any(b.get("reason") == "funding_overheated" for b in (res_hot.get("blocked_entries") or [])))


print("\n[Test 9] battle_report renders both simulators")
import app.futures_lab as fl
orig_fl = fl.load_futures_state
orig_pl = p.load_pro_state
fl.load_futures_state = lambda: {"initial_usdt": 1000.0, "virtual_usdt": 1020.0,
                                 "journal": [{"action": "CLOSE", "pnl_usdt": 20.0}], "active_trade": None}
p.load_pro_state = lambda: {"initial_usdt": 1000.0, "virtual_usdt": 1055.0,
                            "journal": [{"action": "CLOSE", "pnl_usdt": 55.0}], "active_trade": None}
try:
    report = p.battle_report()
finally:
    fl.load_futures_state = orig_fl
    p.load_pro_state = orig_pl
check("battle mentions FUTURES", "FUTURES" in report)
check("battle mentions PRO", "PRO" in report)
check("battle declares PRO ahead (1055 > 1020)", "Впереди PRO" in report)


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
