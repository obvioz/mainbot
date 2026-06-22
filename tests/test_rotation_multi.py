"""Тесты мультипозиционной ротации + расширения вселенной пар.

Запуск: PYTHONPATH=. python tests/test_rotation_multi.py
Сетевых вызовов нет — биржевые функции замоканы.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import app.rotation_lab as rl

results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def mk_open(pair: str) -> dict:
    """Открытая позиция, которая при цене=entry держится (стоп ниже)."""
    return {
        "pair": pair, "entry_price": 100.0, "atr": 1.0,
        "initial_stop": 90.0, "trailing_stop": 90.0,
        "trailing_activated": False, "highest_price": 100.0,
        "btc_amount": 0.33, "opened_at": "2026-06-21T00:00:00+00:00",
    }


def synthetic_best(pair: str) -> dict:
    return {
        "pair": pair, "price": 100.0, "atr": 1.0, "score": 90,
        "pullback_pct": -1.0, "uptrend": True, "volume_ratio": 1.5, "reasons": [],
    }


# --- общая установка моков биржи/состояния ---------------------------------
ALL_PAIRS = ["ETH/BTC", "SOL/BTC", "LTC/BTC", "XRP/BTC"]
_state = {"lab": None, "saved": None}
_find_calls: list = []


def install_mocks(open_on_find: bool):
    _find_calls.clear()
    rl.make_exchange = lambda: object()
    rl.fetch_pair_price = lambda ex, pair: 100.0  # = entry → стоп не срабатывает
    rl.load_rotation_pairs = lambda base="BTC": list(ALL_PAIRS)
    rl.load_rotation_state = lambda: _state["lab"]
    rl.save_rotation_state = lambda lab: _state.__setitem__("saved", lab)

    def find_spy(exchange, lab=None, pairs=None):
        _find_calls.append(list(pairs) if pairs is not None else None)
        if pairs and open_on_find:
            return synthetic_best(pairs[0])
        return None
    rl.find_best_pair = find_spy


# =====================================================================
# 1. Вселенная пар: load_rotation_pairs отсеивает TON/несуществующие
# =====================================================================
print("\n=== Тест 1: фильтрация пар (TON отсеивается) ===")
orig_pairs_file = rl.PAIRS_FILE
with tempfile.TemporaryDirectory() as d:
    pf = Path(d) / "bybit_pairs.json"
    pf.write_text(json.dumps({"BTC": ["ETH/BTC", "TON/BTC", "SOL/BTC", "FAKE/BTC", "LTC/BTC"]}))
    rl.PAIRS_FILE = pf
    loaded = rl.load_rotation_pairs("BTC")
    print("   loaded:", loaded)
    check("TON/BTC отсеян", "TON/BTC" not in loaded)
    check("FAKE/BTC отсеян", "FAKE/BTC" not in loaded)
    check("реальные пары остались", set(loaded) == {"ETH/BTC", "SOL/BTC", "LTC/BTC"}, str(loaded))
rl.PAIRS_FILE = orig_pairs_file


# =====================================================================
# 2. При 0 открытых — ищет вход (по всем парам)
# =====================================================================
print("\n=== Тест 2: 0 открытых → ищет и открывает ===")
install_mocks(open_on_find=True)
_state["lab"] = {"enabled": True, "virtual_btc": 1.0, "open_trades": [],
                 "stop_cooldowns": {}, "journal": []}
res = rl.rotation_tick()
print("   find_best_pair вызван раз:", len(_find_calls), "| free_pairs:", _find_calls[0] if _find_calls else None)
print("   open_trades после тика:", [t["pair"] for t in _state["lab"]["open_trades"]])
check("поиск входа выполнен", len(_find_calls) == 1)
check("свободны все 4 пары", _find_calls[0] == ALL_PAIRS, str(_find_calls[0]))
check("позиция открыта", len(_state["lab"]["open_trades"]) == 1)
check("статус тика opened", res["status"] == "opened", res["status"])


# =====================================================================
# 3. При 3 открытых (= MAX) — НЕ ищет новый вход
# =====================================================================
print("\n=== Тест 3: 3 открытых (MAX) → поиск не выполняется ===")
install_mocks(open_on_find=True)
_state["lab"] = {"enabled": True, "virtual_btc": 1.0,
                 "open_trades": [mk_open("ETH/BTC"), mk_open("SOL/BTC"), mk_open("LTC/BTC")],
                 "stop_cooldowns": {}, "journal": []}
res = rl.rotation_tick()
print("   find_best_pair вызовов:", len(_find_calls), "| открытых:", res["open_count"])
check("MAX_CONCURRENT_ROTATION == 3", rl.MAX_CONCURRENT_ROTATION == 3)
check("поиск НЕ выполнялся при 3 открытых", len(_find_calls) == 0)
check("позиций по-прежнему 3", res["open_count"] == 3, str(res["open_count"]))


# =====================================================================
# 4. При 2 открытых — ищет среди СВОБОДНЫХ пар (не по занятым)
# =====================================================================
print("\n=== Тест 4: 2 открытых → ищет только среди свободных ===")
install_mocks(open_on_find=False)  # не открываем, только смотрим какие пары переданы
_state["lab"] = {"enabled": True, "virtual_btc": 1.0,
                 "open_trades": [mk_open("ETH/BTC"), mk_open("SOL/BTC")],
                 "stop_cooldowns": {}, "journal": []}
res = rl.rotation_tick()
free = _find_calls[0] if _find_calls else []
print("   free_pairs переданные в поиск:", free)
check("поиск выполнен (есть слот)", len(_find_calls) == 1)
check("свободные = LTC, XRP", free == ["LTC/BTC", "XRP/BTC"], str(free))
check("занятые ETH/SOL исключены", "ETH/BTC" not in free and "SOL/BTC" not in free)


# =====================================================================
# 5. Не открывает вторую позицию по уже открытой паре
# =====================================================================
print("\n=== Тест 5: нет второй позиции по той же паре ===")
install_mocks(open_on_find=True)  # find вернёт первую свободную пару
_state["lab"] = {"enabled": True, "virtual_btc": 1.0,
                 "open_trades": [mk_open("SOL/BTC")],
                 "stop_cooldowns": {}, "journal": []}
res = rl.rotation_tick()
pairs_now = [t["pair"] for t in _state["lab"]["open_trades"]]
print("   open_trades:", pairs_now, "| free на входе:", _find_calls[0])
check("SOL/BTC не предлагался для входа", "SOL/BTC" not in _find_calls[0])
check("SOL/BTC ровно одна позиция", pairs_now.count("SOL/BTC") == 1, str(pairs_now))
check("добавилась другая пара", len(pairs_now) == 2 and "SOL/BTC" in pairs_now)


# =====================================================================
# 6. Миграция: старый active_trade → open_trades без потери позиции
# =====================================================================
print("\n=== Тест 6: миграция active_trade → open_trades ===")
legacy_lab = {
    "virtual_btc": 0.99,
    # как в реальном состоянии: старый трейд держит ВЕСЬ баланс в btc_amount
    "active_trade": {"pair": "SOL/BTC", "entry_price": 0.0021, "atr": 0.00001,
                     "initial_stop": 0.002, "trailing_stop": 0.002, "btc_amount": 0.99,
                     "highest_price": 0.0021, "opened_at": "2026-06-20T23:38:00+00:00"},
    "open_trades": [],
}
rl._migrate_active_trade(legacy_lab)
moved = legacy_lab["open_trades"][0]
print("   active_trade:", legacy_lab["active_trade"])
print("   open_trades:", [t["pair"] for t in legacy_lab["open_trades"]],
      "| alloc:", moved.get("btc_amount"), "| free:", round(rl._free_btc(legacy_lab), 6))
check("active_trade обнулён", legacy_lab["active_trade"] is None)
check("SOL/BTC перенесена в open_trades", len(legacy_lab["open_trades"]) == 1
      and moved["pair"] == "SOL/BTC")
check("доля нормализована к 1/3 (не весь баланс)", abs(moved["btc_amount"] - 0.99 / 3) < 1e-9,
      str(moved["btc_amount"]))
check("после миграции освобождён капитал под новые входы", rl._free_btc(legacy_lab) > 0.6)
# повторная миграция не дублирует
rl._migrate_active_trade(legacy_lab)
check("повторная миграция не дублирует", len(legacy_lab["open_trades"]) == 1)


# =====================================================================
# 7. Риск: капитал делится поровну, PnL начисляется на долю
# =====================================================================
print("\n=== Тест 7: деление капитала и PnL по доле ===")
lab = {"virtual_btc": 1.0, "open_trades": [], "journal": [], "stop_cooldowns": {}}
for p in ("ETH/BTC", "SOL/BTC", "LTC/BTC"):
    rl.open_virtual_trade(lab, synthetic_best(p))
allocs = [round(t["btc_amount"], 4) for t in lab["open_trades"]]
print("   allocations:", allocs, "| free:", round(rl._free_btc(lab), 6))
check("3 позиции по равной доле ~0.333", allocs == [0.3333, 0.3333, 0.3333], str(allocs))
check("свободный остаток ~0", abs(rl._free_btc(lab)) < 1e-6, str(rl._free_btc(lab)))

# закрытие одной позиции на +10% начисляет PnL только на её долю
lab2 = {"virtual_btc": 1.0, "journal": [], "stop_cooldowns": {}}
trade = {"pair": "ETH/BTC", "entry_price": 100.0, "btc_amount": 1.0 / 3,
         "initial_stop": 90.0, "trailing_stop": 90.0, "highest_price": 110.0,
         "opened_at": "2026-06-21T00:00:00+00:00"}
lab2["open_trades"] = [trade]
rl.close_virtual_trade(lab2, trade, 110.0, "trailing_stop")
print("   virtual_btc после +10%% на 1/3 капитала:", round(lab2["virtual_btc"], 6))
check("PnL начислен на долю (1.0+1/3*0.10)", abs(lab2["virtual_btc"] - (1.0 + (1 / 3) * 0.10)) < 1e-9,
      str(lab2["virtual_btc"]))
check("позиция удалена из open_trades", len(lab2["open_trades"]) == 0)


# --- Итог ---
print("\n" + "=" * 55)
passed = sum(1 for _, c, _ in results if c)
total = len(results)
print(f"ИТОГО: {passed}/{total} проверок прошло")
sys.exit(0 if passed == total else 1)
