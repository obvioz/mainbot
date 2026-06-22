"""Тесты интеграции персональных ATR-уровней входа в signals.py.

Запуск: PYTHONPATH=. python tests/test_entry_levels.py
get_profile замокан — сети нет.
"""
from __future__ import annotations

import sys

import app.signals as sig
import app.volatility_profile as vp
from app.signals import get_entry_levels_for_coin, classify_signal, FALLBACK_ENTRY_LEVELS
from app.coin_health import blocks_entry

results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def patch_profile(profile):
    # get_entry_levels_for_coin живёт в volatility_profile и читает vp.get_profile.
    vp.get_profile = lambda coin: profile
    sig.get_profile = lambda coin: profile


def make_item(coin="TST", dd30=-22.0, ch7=-6.0, ch24=-2.0, atr=5.0, current=1.0):
    return {"coin": coin, "change_24h": ch24, "change_7d": ch7,
            "drawdown_30d_high": dd30, "atr_pct": atr, "volatility_class": "MEDIUM",
            "current": current, "derivatives": {}, "news_risk": {}}


# =====================================================================
# 1. BTC CALM → узкие уровни из профиля
# =====================================================================
print("\n=== Тест 1: CALM → узкие уровни из профиля ===")
patch_profile({"class": "CALM", "vol_regime": "NORMAL", "atr_short_pct": 3.2,
               "entry_levels": [3.2, 6.4, 11.2]})
r = get_entry_levels_for_coin("BTC")
print("   ", r["levels"], "| source:", r["source"], "| missing:", r["profile_missing"])
check("уровни из профиля (не дефолт)", r["levels"] == [3.2, 6.4, 11.2], str(r["levels"]))
check("CALM узкий т1 < 5%", r["levels"][0] < 5.0)
check("source=profile, не missing", r["source"] == "profile" and r["profile_missing"] is False)
check("не клампилось", r["clamped"] is False)


# =====================================================================
# 2. FET WILD → широкие уровни
# =====================================================================
print("\n=== Тест 2: WILD → широкие уровни ===")
patch_profile({"class": "WILD", "vol_regime": "NORMAL", "atr_short_pct": 8.0,
               "entry_levels": [8.0, 16.0, 28.0]})
w = get_entry_levels_for_coin("FET")
print("   ", w["levels"])
check("WILD широкий т1 > 5%", w["levels"][0] > 5.0, str(w["levels"][0]))
check("WILD т3 > 20%", w["levels"][2] > 20.0)
check("WILD уровни шире CALM", all(a > b for a, b in zip(w["levels"], r["levels"])))


# =====================================================================
# 3. Нет профиля → fallback, не падает
# =====================================================================
print("\n=== Тест 3: нет профиля → fallback ===")
patch_profile(None)
f = get_entry_levels_for_coin("NEWCOIN")
print("   ", f["levels"], "| missing:", f["profile_missing"])
check("fallback дефолты", f["levels"] == FALLBACK_ENTRY_LEVELS, str(f["levels"]))
check("profile_missing=True", f["profile_missing"] is True)
check("source=fallback", f["source"] == "fallback")


# =====================================================================
# 4. Клампинг min: т1 -1% → -2%
# =====================================================================
print("\n=== Тест 4: кламп min (т1 -1% → -2%) ===")
patch_profile({"class": "CALM", "vol_regime": "QUIET", "atr_short_pct": 1.0,
               "entry_levels": [1.0, 2.0, 3.5]})
c1 = get_entry_levels_for_coin("CALMEST")
print("   ", c1["levels"], "| clamped:", c1["clamped"])
check("т1 клампится до 2%", c1["levels"][0] == 2.0, str(c1["levels"][0]))
check("флаг clamped=True", c1["clamped"] is True)
check("лесенка строго возрастает", c1["levels"][0] < c1["levels"][1] < c1["levels"][2], str(c1["levels"]))


# =====================================================================
# 5. Клампинг max: т3 -60% → -45%
# =====================================================================
print("\n=== Тест 5: кламп max (т3 -60% → -45%) ===")
patch_profile({"class": "WILD", "vol_regime": "EXPANDING", "atr_short_pct": 20.0,
               "entry_levels": [10.0, 20.0, 60.0]})
c2 = get_entry_levels_for_coin("WILDEST")
print("   ", c2["levels"], "| clamped:", c2["clamped"])
check("т3 клампится до 45%", c2["levels"][2] == 45.0, str(c2["levels"][2]))
check("флаг clamped=True", c2["clamped"] is True)


# =====================================================================
# 6. EXPANDING → планка score поднята на +10
# =====================================================================
print("\n=== Тест 6: EXPANDING поднимает требование score на +10 ===")
mc = {"score": 60, "mode": "SIDEWAYS"}
base_levels = [8.0, 16.0, 28.0]

patch_profile({"class": "WILD", "vol_regime": "NORMAL", "atr_short_pct": 8.0, "entry_levels": base_levels})
sig_normal = classify_signal(make_item(), None, mc)

patch_profile({"class": "WILD", "vol_regime": "EXPANDING", "atr_short_pct": 8.0, "entry_levels": base_levels})
sig_exp = classify_signal(make_item(), None, mc)

print("   score NORMAL:", sig_normal["score"], "| EXPANDING:", sig_exp["score"])
check("EXPANDING score ниже на 10", sig_normal["score"] - sig_exp["score"] == 10,
      f"{sig_normal['score']} vs {sig_exp['score']}")
check("в EXPANDING есть пометка ⚡", any("EXPANDING" in str(x) for x in sig_exp["reasons"]))
check("vol_levels проброшен в сигнал", sig_exp.get("vol_levels", {}).get("vol_regime") == "EXPANDING")


# =====================================================================
# 7. coin_health ANOMALY всё ещё блокирует вход (не сломали)
# =====================================================================
print("\n=== Тест 7: coin_health ANOMALY/DEAD блокируют (регрессия) ===")
check("ANOMALY блокирует", blocks_entry({"status": "ANOMALY"}) is True)
check("DEAD блокирует", blocks_entry({"status": "DEAD"}) is True)
check("WARNING НЕ блокирует", blocks_entry({"status": "WARNING"}) is False)


# =====================================================================
# 8. classify_signal не падает без профиля (fallback в реальном потоке)
# =====================================================================
print("\n=== Тест 8: classify_signal с fallback не падает ===")
patch_profile(None)
sig_fb = classify_signal(make_item(), None, mc)
print("   status:", sig_fb["status"], "| levels:", sig_fb["vol_levels"]["levels"])
check("сигнал сгенерирован", sig_fb.get("status") in {"STRONG_BUY", "ACCUMULATION", "WATCH", "AVOID"})
check("использован fallback", sig_fb["vol_levels"]["profile_missing"] is True)


# --- Итог ---
print("\n" + "=" * 55)
passed = sum(1 for _, c, _ in results if c)
total = len(results)
print(f"ИТОГО: {passed}/{total} проверок прошло")
sys.exit(0 if passed == total else 1)
