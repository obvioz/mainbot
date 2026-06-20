"""Тесты профилировщика волатильности.

Запуск: PYTHONPATH=. python tests/test_volatility_profile.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import app.volatility_profile as vp
from app.volatility_profile import (
    compute_profile,
    classify_coin,
    classify_regime,
    entry_levels_pct,
    build_profile_from_ohlcv,
    profiles_need_refresh,
    format_volprofiles_table,
)

results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# --- синтетические дневные свечи с заданным дневным диапазоном (% от цены) ---
def make_ohlcv(days: int, price: float, daily_range_pct: float) -> list[list]:
    rows = []
    rng = price * daily_range_pct / 100.0
    for i in range(days):
        high = price + rng / 2
        low = price - rng / 2
        rows.append([i, price, high, low, price, 1000.0])
    return rows


# =====================================================================
# 1. Классификация: CALM / MEDIUM / WILD
# =====================================================================
print("\n=== Тест 1: классы по длинному ATR ===")
check("ATR 2% → CALM", classify_coin(2.0) == "CALM", classify_coin(2.0))
check("ATR 4% → MEDIUM", classify_coin(4.0) == "MEDIUM", classify_coin(4.0))
check("ATR 9% → WILD", classify_coin(9.0) == "WILD", classify_coin(9.0))


# =====================================================================
# 2. entry_levels: CALM уже, чем WILD
# =====================================================================
print("\n=== Тест 2: уровни CALM уже, чем WILD ===")
calm = entry_levels_pct(2.5)   # ~ [2.5, 5.0, 8.8]
wild = entry_levels_pct(8.0)   # ~ [8.0, 16.0, 28.0]
print("   CALM levels:", calm, "| WILD levels:", wild)
check("CALM множители = 1/2/3.5×ATR", calm == [2.5, 5.0, 8.8], str(calm))
check("каждый уровень CALM < WILD", all(c < w for c, w in zip(calm, wild)))


# =====================================================================
# 3. Режим волатильности: short = 2× long → EXPANDING
# =====================================================================
print("\n=== Тест 3: режим волатильности ===")
check("ratio 2.0 → EXPANDING", classify_regime(2.0) == "EXPANDING", classify_regime(2.0))
check("ratio 1.0 → NORMAL", classify_regime(1.0) == "NORMAL", classify_regime(1.0))
check("ratio 0.5 → QUIET", classify_regime(0.5) == "QUIET", classify_regime(0.5))

p = compute_profile("MOCK", atr_long_pct=4.0, atr_short_pct=8.0)
print("   compute_profile(long=4, short=8):", {"ratio": p["ratio"], "vol_regime": p["vol_regime"], "class": p["class"]})
check("профиль short=2×long → EXPANDING", p["vol_regime"] == "EXPANDING", p["vol_regime"])
check("профиль ratio = 2.0", p["ratio"] == 2.0, str(p["ratio"]))


# =====================================================================
# 3b. build_profile_from_ohlcv: спокойная пара = CALM, дикая = WILD
# =====================================================================
print("\n=== Тест 3b: профиль из свечей (CALM vs WILD) ===")
calm_prof = build_profile_from_ohlcv("CALMC", make_ohlcv(200, 100.0, 2.0))   # ~2% диапазон
wild_prof = build_profile_from_ohlcv("WILDC", make_ohlcv(200, 100.0, 10.0))  # ~10% диапазон
print("   CALM:", calm_prof["class"], "long=", calm_prof["atr_long_pct"], "levels=", calm_prof["entry_levels"])
print("   WILD:", wild_prof["class"], "long=", wild_prof["atr_long_pct"], "levels=", wild_prof["entry_levels"])
check("спокойная пара → CALM", calm_prof["class"] == "CALM", calm_prof["class"])
check("дикая пара → WILD", wild_prof["class"] == "WILD", wild_prof["class"])
check("уровни CALM уже WILD",
      all(c < w for c, w in zip(calm_prof["entry_levels"], wild_prof["entry_levels"])))
check("recent_high посчитан", calm_prof["recent_high"] is not None)


# =====================================================================
# 4. Автопересчёт: старый профиль → refresh, свежий → нет
# =====================================================================
print("\n=== Тест 4: автопересчёт по возрасту файла ===")
orig_path = vp.PROFILES_PATH
with tempfile.TemporaryDirectory() as d:
    # 4a. файла нет → нужен refresh
    vp.PROFILES_PATH = Path(d) / "no_such.json"
    check("нет файла → нужен refresh", profiles_need_refresh() is True)

    # 4b. свежий профиль (сегодня) → refresh НЕ нужен
    fresh = {"updated_at": datetime.now().isoformat(timespec="seconds"), "profiles": {"BTC": calm_prof}}
    vp.PROFILES_PATH = Path(d) / "fresh.json"
    vp._atomic_write_json(vp.PROFILES_PATH, fresh)
    check("свежий профиль (0д) → refresh НЕ нужен", profiles_need_refresh() is False)

    # 4c. профиль старше 7 дней → нужен refresh
    old_ts = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
    old = {"updated_at": old_ts, "profiles": {"BTC": calm_prof}}
    vp.PROFILES_PATH = Path(d) / "old.json"
    vp._atomic_write_json(vp.PROFILES_PATH, old)
    check("профиль >7д → нужен refresh", profiles_need_refresh() is True)
vp.PROFILES_PATH = orig_path


# =====================================================================
# 5. /volprofiles рендерит таблицу
# =====================================================================
print("\n=== Тест 5: таблица /volprofiles ===")
store = {
    "updated_at": "2026-06-20T10:00:00",
    "profiles": {"BTC": calm_prof, "WILDC": wild_prof},
    "errors": {},
}
table = format_volprofiles_table(store)
print(table)
check("таблица содержит монеты", "CALMC" in table and "WILDC" in table)
check("таблица содержит классы", "CALM" in table and "WILD" in table)
check("таблица содержит уровни входа", "%" in table)


# --- Итог ---
print("\n" + "=" * 55)
passed = sum(1 for _, c, _ in results if c)
total = len(results)
print(f"ИТОГО: {passed}/{total} проверок прошло")
sys.exit(0 if passed == total else 1)
