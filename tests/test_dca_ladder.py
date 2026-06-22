"""Тесты: пост-входная лесенка доборов на персональных ATR-уровнях.

Проверяет единый источник уровней для всей цепочки и миграцию старых позиций,
не трогая money-логику. Storage перенаправлен на временные файлы — живое
состояние не затрагивается.

Запуск: PYTHONPATH=. python tests/test_dca_ladder.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import app.storage as st
import app.volatility_profile as vp
import app.signals as sig

results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# --- временный storage ------------------------------------------------------
_tmp = tempfile.TemporaryDirectory()
_d = Path(_tmp.name)
st.PORTFOLIO_PATH = _d / "portfolio_state.json"
st.JOURNAL_PATH = _d / "journal.json"
st.PORTFOLIO_LOCK_PATH = _d / "portfolio_state.json.lock"
st.JOURNAL_LOCK_PATH = _d / "journal.json.lock"


def reset_state(positions: dict | None = None):
    st._atomic_write_json(st.PORTFOLIO_PATH, {"positions": positions or {}, "alerts": {}})


def patch_profiles(mapping: dict):
    vp.get_profile = lambda coin: mapping.get(coin)


PROFILES = {
    "BTC": {"class": "MEDIUM", "vol_regime": "NORMAL", "atr_short_pct": 3.2, "entry_levels": [3.2, 6.4, 11.2]},
    "SOL": {"class": "WILD", "vol_regime": "NORMAL", "atr_short_pct": 5.3, "entry_levels": [5.3, 10.6, 18.6]},
}
patch_profiles(PROFILES)


# =====================================================================
# 1. Открытие позиции → next_buy от персонального уровня, не от старых dca
# =====================================================================
print("\n=== Тест 1: первый вход → персональный next_buy ===")
reset_state()
pos = st.record_buy("BTC", 100.0, 60000.0, reason="manual")  # dca_levels не передаём
print("   dca_levels_used:", pos["dca_levels_used"], "| source:", pos.get("levels_source"))
print("   next_buy_drop_pct:", pos["next_buy_drop_pct"], "| next_buy_price:", pos["next_buy_price"])
check("уровни = персональные ATR", pos["dca_levels_used"] == [3.2, 6.4, 11.2], str(pos["dca_levels_used"]))
check("levels_source=personal_atr", pos.get("levels_source") == "personal_atr")
check("next drop = т2 (6.4%)", pos["next_buy_drop_pct"] == 6.4, str(pos["next_buy_drop_pct"]))
check("next_buy = entry*(1-6.4%)", abs(pos["next_buy_price"] - 60000 * (1 - 0.064)) < 1e-6, str(pos["next_buy_price"]))


# =====================================================================
# 2. Добор (транш 2) → средняя верна, следующий уровень тоже персональный (т3)
# =====================================================================
print("\n=== Тест 2: добор → avg верна + следующий уровень т3 ===")
pos = st.record_buy("BTC", 100.0, 56160.0, reason="manual")  # второй вход
qty = float(pos["qty"]); invested = float(pos["invested_usdt"]); avg = float(pos["avg_price"])
print("   qty:", round(qty, 8), "| invested:", invested, "| avg:", round(avg, 2))
check("entry_count = 2", int(pos["entry_count"]) == 2, str(pos["entry_count"]))
check("avg = invested/qty (не сломана)", abs(avg - invested / qty) < 1e-6)
check("avg между ценами входов", 56160.0 < avg < 60000.0, str(avg))
check("invested = 200", abs(invested - 200.0) < 1e-9)
check("следующий уровень = т3 (11.2%)", pos["next_buy_drop_pct"] == 11.2, str(pos["next_buy_drop_pct"]))
check("next_buy = last_entry*(1-11.2%)", abs(pos["next_buy_price"] - 56160.0 * (1 - 0.112)) < 1e-3)


# =====================================================================
# 3. Существующая позиция со СТАРЫМИ уровнями → миграция без потери
# =====================================================================
print("\n=== Тест 3: миграция старой позиции ===")
reset_state({
    "SOL": {
        "coin": "SOL", "qty": 2.0, "invested_usdt": 300.0, "avg_price": 150.0,
        "entry_count": 1, "last_entry_price": 150.0,
        "dca_levels_used": [15.0, 25.0, 40.0],  # СТАРЫЕ фиксированные уровни
        "next_buy_drop_pct": 25.0, "next_buy_price": 112.5,
        "entries": [{"price": 150.0, "amount_usdt": 300.0, "qty": 2.0}],
    }
})
n = st.migrate_positions_to_personal_levels()
p = st.get_open_positions()["SOL"]
print("   migrated:", n, "| new levels:", p["dca_levels_used"], "| next_drop:", p["next_buy_drop_pct"], "| next_buy:", p["next_buy_price"])
check("мигрирована 1 позиция", n == 1, str(n))
check("уровни → персональные", p["dca_levels_used"] == [5.3, 10.6, 18.6], str(p["dca_levels_used"]))
check("next drop = т2 (10.6%)", p["next_buy_drop_pct"] == 10.6, str(p["next_buy_drop_pct"]))
check("next_buy = last_entry*(1-10.6%)", abs(p["next_buy_price"] - 150.0 * (1 - 0.106)) < 1e-6)
# money не тронуты
check("qty не изменилась", float(p["qty"]) == 2.0)
check("invested не изменился", float(p["invested_usdt"]) == 300.0)
check("avg не изменилась", float(p["avg_price"]) == 150.0)
# идемпотентность
n2 = st.migrate_positions_to_personal_levels()
check("повторная миграция = 0 (идемпотентно)", n2 == 0, str(n2))


# =====================================================================
# 4. Реконсиляция продаж не сломана: record_sell уменьшает позицию корректно
#    (money-логика остатка/avg при частичной продаже)
# =====================================================================
print("\n=== Тест 4: частичная продажа (money-логика остатка) ===")
reset_state()
st.record_buy("BTC", 100.0, 60000.0, reason="manual")   # qty ~0.0016667
before = st.get_open_positions()["BTC"]
qty_before = float(before["qty"])
st.record_sell("BTC", 66000.0, amount_usdt=50.0)         # продать ~50 USDT
after = st.get_open_positions().get("BTC")
print("   qty:", round(qty_before, 8), "->", round(float(after["qty"]), 8) if after else None)
check("позиция не потеряна", after is not None)
check("qty уменьшилась", after and float(after["qty"]) < qty_before)
check("avg_price > 0 после продажи", after and float(after["avg_price"]) > 0)


# =====================================================================
# 5. Согласованность: сигнал == превью == лесенка доборов (одна монета)
# =====================================================================
print("\n=== Тест 5: согласованность уровней по всей цепочке (SOL) ===")
reset_state()
item = {"coin": "SOL", "change_24h": -3.0, "change_7d": -8.0, "drawdown_30d_high": -25.0,
        "atr_pct": 6.0, "volatility_class": "HIGH", "current": 150.0,
        "derivatives": {}, "news_risk": {}}
signal = sig.classify_signal(item, None, {"score": 60, "mode": "SIDEWAYS"})
sig_levels = signal["vol_levels"]["levels"]
canonical = vp.get_entry_levels_for_coin("SOL")["levels"]
pos_sol = st.record_buy("SOL", 100.0, 150.0, reason="manual")
ladder_levels = pos_sol["dca_levels_used"]
# превью следующего уровня (no-position путь использует т2 = levels[1])
preview_pct = sig_levels[1]
print("   signal:", sig_levels, "| canonical:", canonical, "| ladder:", ladder_levels, "| preview т2:", preview_pct)
check("сигнал == canonical", sig_levels == canonical, f"{sig_levels} vs {canonical}")
check("лесенка == canonical", ladder_levels == canonical, f"{ladder_levels} vs {canonical}")
check("превью т2 == уровень добора т2", preview_pct == ladder_levels[1])


# --- Итог ---
print("\n" + "=" * 55)
passed = sum(1 for _, c, _ in results if c)
total = len(results)
print(f"ИТОГО: {passed}/{total} проверок прошло")
_tmp.cleanup()
sys.exit(0 if passed == total else 1)
