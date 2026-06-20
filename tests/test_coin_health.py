"""Тесты health-check монеты и регрессия по news_risk (Task 1).

Запуск: python tests/test_coin_health.py
Не использует pytest, чтобы не тянуть зависимостей.
"""
from __future__ import annotations

import sys
import traceback

from app.coin_health import health_check, blocks_entry
from app.signals import _risk_state


# --- Duck-typed фейк биржи под ccxt-интерфейс, который трогает coin_health ---
class FakeExchange:
    def __init__(self, markets: dict, ticker: dict, ohlcv: list):
        self.markets = markets
        self._ticker = ticker
        self._ohlcv = ohlcv

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        m = self.markets.get(symbol)
        if m is None:
            raise ValueError(f"no market {symbol}")
        return m

    def fetch_ticker(self, symbol):
        if "BTC/" in symbol and symbol not in self.markets:
            # запрос BTC для сравнения аномалии — отдаём стабильный BTC
            return {"percentage": 0.0, "bid": 60000, "ask": 60001, "last": 60000}
        return dict(self._ticker)

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=31):
        return [list(r) for r in self._ohlcv]


def _ohlcv(vols: list[float], price: float = 100.0) -> list:
    # [ts, o, h, l, c, v]
    return [[i, price, price, price, price, v] for i, v in enumerate(vols)]


def _healthy_market(symbol: str, status="Trading", active=True) -> dict:
    return {symbol: {"active": active, "info": {"status": status}}}


results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


# =====================================================================
# 1. Активная ликвидная пара (BTC) — ЖИВОЙ вызов биржи → HEALTHY
# =====================================================================
print("\n=== Тест 1: BTC live → HEALTHY, вход разрешён ===")
try:
    h = health_check("BTC")
    print("   status:", h["status"], "| reasons:", h["reasons"])
    print("   details:", h["details"])
    check("BTC live = HEALTHY", h["status"] == "HEALTHY", h["status"])
    check("BTC не блокирует вход", blocks_entry(h) is False)
except Exception:
    traceback.print_exc()
    check("BTC live = HEALTHY", False, "exception")


# =====================================================================
# 2a. Делистинг: пары нет в списке → DEAD
# =====================================================================
print("\n=== Тест 2a: пара отсутствует (делистинг) → DEAD, блок ===")
ex = FakeExchange(markets={}, ticker={"percentage": 0, "bid": 1, "ask": 1.01},
                  ohlcv=_ohlcv([100] * 31))
h = health_check("DEADCOIN", exchange=ex)
print("   status:", h["status"], "| reasons:", h["reasons"])
check("delisted = DEAD", h["status"] == "DEAD", h["status"])
check("DEAD блокирует вход", blocks_entry(h) is True)


# =====================================================================
# 2b. Suspended/halt: пара есть, но статус не Trading → DEAD
# =====================================================================
print("\n=== Тест 2b: пара suspended (status != Trading) → DEAD, блок ===")
ex = FakeExchange(markets={"FOO/USDT": {"active": False, "info": {"status": "Settling"}}},
                  ticker={"percentage": 0, "bid": 1, "ask": 1.01},
                  ohlcv=_ohlcv([100] * 31))
h = health_check("FOO", exchange=ex)
print("   status:", h["status"], "| reasons:", h["reasons"])
check("suspended = DEAD", h["status"] == "DEAD", h["status"])
check("suspended блокирует вход", blocks_entry(h) is True)


# =====================================================================
# 3. Схлопнувшийся объём: 24h << среднего за 30д → WARNING (вход разрешён)
# =====================================================================
print("\n=== Тест 3: объём схлопнулся → WARNING, вход разрешён ===")
# История: ~100000 quote/день (1000 base * 100). Тикер 24h объём 5000 → ratio 0.05 < 0.20
ex = FakeExchange(markets=_healthy_market("BAR/USDT"),
                  ticker={"percentage": -2.0, "bid": 100, "ask": 100.1, "quoteVolume": 5000.0},
                  ohlcv=_ohlcv([1000.0] * 31))
h = health_check("BAR", exchange=ex)
print("   status:", h["status"], "| reasons:", h["reasons"])
print("   vol_ratio:", h["details"].get("volume_ratio_24h_vs_30d"))
check("объём схлопнулся = WARNING", h["status"] == "WARNING", h["status"])
check("WARNING НЕ блокирует вход", blocks_entry(h) is False)


# =====================================================================
# 4. Аномалия: монета -20%, BTC 0% → ANOMALY (блок)
# =====================================================================
print("\n=== Тест 4: монета -20% при BTC 0% → ANOMALY, блок ===")
ex = FakeExchange(markets=_healthy_market("BAZ/USDT"),
                  ticker={"percentage": -20.0, "bid": 80, "ask": 80.1},
                  ohlcv=_ohlcv([1000.0] * 31))
h = health_check("BAZ", exchange=ex, btc_change_24h=0.0)
print("   status:", h["status"], "| reasons:", h["reasons"])
check("монета -20% / BTC 0% = ANOMALY", h["status"] == "ANOMALY", h["status"])
check("ANOMALY блокирует вход", blocks_entry(h) is True)

# 4b. Контроль: монета -20% но BTC тоже -18% (общий пролив) → НЕ аномалия
print("\n=== Тест 4b: монета -20% при BTC -18% (рынок) → НЕ ANOMALY ===")
ex = FakeExchange(markets=_healthy_market("BAZ/USDT"),
                  ticker={"percentage": -20.0, "bid": 80, "ask": 80.1},
                  ohlcv=_ohlcv([1000.0] * 31))
h = health_check("BAZ", exchange=ex, btc_change_24h=-18.0)
print("   status:", h["status"], "| reasons:", h["reasons"])
check("общий пролив != ANOMALY", h["status"] != "ANOMALY", h["status"])


# =====================================================================
# 5. Широкий спред → WARNING
# =====================================================================
print("\n=== Тест 5: широкий спред bid/ask → WARNING ===")
ex = FakeExchange(markets=_healthy_market("WIDE/USDT"),
                  ticker={"percentage": -1.0, "bid": 100, "ask": 103},  # ~3% спред
                  ohlcv=_ohlcv([1000.0] * 31))
h = health_check("WIDE", exchange=ex)
print("   status:", h["status"], "| reasons:", h["reasons"], "| spread:", h["details"].get("spread_pct"))
check("широкий спред = WARNING", h["status"] == "WARNING", h["status"])


# =====================================================================
# 6. Регрессия Task 1: HIGH news по альту больше НЕ даёт hard_avoid
# =====================================================================
print("\n=== Тест 6 (Task 1): HIGH news по альту НЕ блокирует вход ===")
rs = _risk_state({}, {"state": "HIGH", "comment": "тест"}, False, "SOL")
print("   risk_state:", {"score": rs["score"], "hard_avoid": rs["hard_avoid"]})
check("HIGH news SOL: hard_avoid=False", rs["hard_avoid"] is False)
check("HIGH news всё ещё штрафует score", rs["score"] < 75, f"score={rs['score']}")


# --- Итог ---
print("\n" + "=" * 55)
passed = sum(1 for _, c, _ in results if c)
total = len(results)
print(f"ИТОГО: {passed}/{total} проверок прошло")
sys.exit(0 if passed == total else 1)
