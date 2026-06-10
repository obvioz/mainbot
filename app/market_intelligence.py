from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

CACHE_PATH = Path("data/market_intelligence_cache.json")


def _read_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _is_fresh(row: dict, minutes: int) -> bool:
    try:
        ts = datetime.fromisoformat(row.get("ts", ""))
        return datetime.now() - ts < timedelta(minutes=minutes)
    except Exception:
        return False


def _get_json_url(url: str, timeout: int = 8) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-invest-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_fear_greed() -> dict:
    """Fear & Greed от alternative.me. Если API недоступно — возвращаем neutral."""
    cache = _read_cache()
    cached = cache.get("fear_greed")
    if cached and _is_fresh(cached, 180):
        return cached["value"]
    try:
        data = _get_json_url("https://api.alternative.me/fng/?limit=1&format=json")
        row = data.get("data", [{}])[0]
        value = int(row.get("value", 50))
        result = {
            "value": value,
            "classification": row.get("value_classification", "Neutral"),
            "source": "alternative.me",
        }
    except Exception:
        result = {"value": 50, "classification": "Neutral", "source": "fallback"}
    cache["fear_greed"] = {"ts": datetime.now().isoformat(timespec="seconds"), "value": result}
    _write_cache(cache)
    return result


def fetch_btc_dominance() -> dict:
    """BTC dominance через CoinGecko Global. Если недоступно — neutral/fallback."""
    cache = _read_cache()
    cached = cache.get("btc_dominance")
    if cached and _is_fresh(cached, 180):
        return cached["value"]
    try:
        data = _get_json_url("https://api.coingecko.com/api/v3/global")
        pct = float(data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0))
        result = {"value": pct, "source": "coingecko"}
    except Exception:
        result = {"value": 0.0, "source": "fallback"}
    cache["btc_dominance"] = {"ts": datetime.now().isoformat(timespec="seconds"), "value": result}
    _write_cache(cache)
    return result


def _ema(series: pd.Series, span: int) -> float:
    if len(series) < span:
        return float(series.iloc[-1])
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


def analyze_btc_trend(exchange, fetch_ohlcv_df_func) -> dict:
    """Тренд BTC: 200 EMA + структура 30 дней."""
    try:
        df = fetch_ohlcv_df_func(exchange, "BTC/USDT", "1d", 220)
        closes = df["close"].astype(float)
        current = float(closes.iloc[-1])
        ema200 = _ema(closes, 200)
        ema50 = _ema(closes, 50)
        above_200 = current >= ema200
        above_50 = current >= ema50
        ch30 = (current / float(closes.iloc[-31]) - 1) * 100 if len(closes) > 31 else 0.0
        trend = "BULL" if above_200 and above_50 else "WEAK" if above_200 else "BEAR"
        return {
            "current": current,
            "ema200": ema200,
            "ema50": ema50,
            "above_200": above_200,
            "above_50": above_50,
            "change_30d": ch30,
            "trend": trend,
        }
    except Exception as exc:
        return {"trend": "UNKNOWN", "error": str(exc)}


def analyze_alt_basket(items: list[dict]) -> dict:
    """Прокси TOTAL3 на нашем списке: сколько альтов сильнее BTC за 7 дней."""
    btc = next((x for x in items if x.get("coin") == "BTC" and "error" not in x), None)
    if not btc:
        return {"state": "UNKNOWN", "strong_count": 0, "total": 0, "avg_7d": 0, "source": "basket"}
    btc7 = float(btc.get("change_7d", 0))
    alts = [x for x in items if x.get("coin") not in {"BTC"} and "error" not in x]
    if not alts:
        return {"state": "UNKNOWN", "strong_count": 0, "total": 0, "avg_7d": 0, "source": "basket"}
    strong = [x for x in alts if float(x.get("change_7d", 0)) >= btc7]
    avg7 = sum(float(x.get("change_7d", 0)) for x in alts) / len(alts)
    ratio = len(strong) / len(alts)
    if ratio >= 0.65 and avg7 > -3:
        state = "ALT_STRENGTH"
    elif ratio <= 0.30 or avg7 < -8:
        state = "ALT_WEAKNESS"
    else:
        state = "NEUTRAL"
    return {"state": state, "strong_count": len(strong), "total": len(alts), "avg_7d": avg7, "btc_7d": btc7, "source": "basket"}


def _score_fear_greed(fng: dict) -> tuple[int, str]:
    v = int(fng.get("value", 50))
    # Для нашей стратегии страх лучше жадности, но экстремальный страх в медвежке опасен.
    if 25 <= v <= 45:
        return 12, "страх/осторожность — есть смысл искать просадки"
    if v < 25:
        return 6, "экстремальный страх — входы только частями"
    if 46 <= v <= 65:
        return 8, "нейтральное настроение"
    return 2, "жадность — покупки хуже по risk/reward"


def build_market_context(items: list[dict], exchange=None, fetch_ohlcv_df_func=None) -> dict:
    btc_item = next((x for x in items if x.get("coin") == "BTC" and "error" not in x), None)
    fng = fetch_fear_greed()
    dominance = fetch_btc_dominance()
    alt_basket = analyze_alt_basket(items)

    btc_trend = {"trend": "UNKNOWN"}
    if exchange is not None and fetch_ohlcv_df_func is not None:
        btc_trend = analyze_btc_trend(exchange, fetch_ohlcv_df_func)

    score = 50
    reasons: list[str] = []

    # BTC short-term state
    if btc_item:
        ch24 = float(btc_item.get("change_24h", 0))
        ch7 = float(btc_item.get("change_7d", 0))
        dd30 = float(btc_item.get("drawdown_30d_high", 0))
        if ch24 <= -6:
            score -= 18; reasons.append("BTC сильный дневной пролив")
        elif ch24 <= -3:
            score -= 8; reasons.append("BTC дневная слабость")
        elif ch24 >= 2:
            score += 5; reasons.append("BTC краткосрочно силен")
        if ch7 >= 5:
            score += 10; reasons.append("BTC растет за 7 дней")
        elif ch7 <= -8:
            score -= 12; reasons.append("BTC слаб за 7 дней")
        if dd30 <= -18:
            score -= 10; reasons.append("BTC глубоко ниже 30д high")
        elif dd30 > -6:
            score += 7; reasons.append("BTC близко к 30д high")

    # BTC 200 EMA trend
    trend = btc_trend.get("trend")
    if trend == "BULL":
        score += 18; reasons.append("BTC выше EMA50/EMA200")
    elif trend == "WEAK":
        score += 4; reasons.append("BTC выше EMA200, но структура слабее")
    elif trend == "BEAR":
        score -= 22; reasons.append("BTC ниже EMA200")

    # Fear & greed
    fng_score, fng_reason = _score_fear_greed(fng)
    score += fng_score - 7
    reasons.append(fng_reason)

    # Dominance absolute interpretation (without history)
    dom = float(dominance.get("value", 0) or 0)
    if dom:
        if dom >= 58:
            score -= 3; reasons.append(f"BTC dominance высокая ({dom:.1f}%) — альты осторожно")
        elif dom <= 48:
            score += 4; reasons.append(f"BTC dominance ниже ({dom:.1f}%) — альтам легче")
        else:
            reasons.append(f"BTC dominance нейтральная ({dom:.1f}%)")

    # Alt basket proxy
    if alt_basket.get("state") == "ALT_STRENGTH":
        score += 8; reasons.append("альт-корзина сильнее BTC")
    elif alt_basket.get("state") == "ALT_WEAKNESS":
        score -= 10; reasons.append("альт-корзина слабая")

    score = max(0, min(100, int(round(score))))
    if score >= 80:
        mode = "BULL"
        recommendation = "Можно активнее искать просадки, но без FOMO."
    elif score >= 65:
        mode = "HEALTHY CORRECTION"
        recommendation = "Лучший режим для частичных входов в сильные монеты."
    elif score >= 50:
        mode = "SIDEWAYS"
        recommendation = "Работаем сеткой, входы небольшими частями."
    elif score >= 35:
        mode = "RISK-OFF"
        recommendation = "Осторожно: приоритет BTC/ETH и кэш, альты минимально."
    else:
        mode = "BEAR / PANIC"
        recommendation = "Новые покупки только после ручной проверки, агрессивное усреднение запрещено."

    return {
        "score": score,
        "mode": mode,
        "recommendation": recommendation,
        "reasons": reasons[:8],
        "fear_greed": fng,
        "btc_dominance": dominance,
        "btc_trend": btc_trend,
        "alt_basket": alt_basket,
    }


def coin_strength_vs_btc(item: dict, btc_item: dict | None) -> dict:
    if not btc_item or "error" in item or "error" in btc_item or item.get("coin") == "BTC":
        return {"delta_7d": 0.0, "state": "BASE"}
    delta = float(item.get("change_7d", 0)) - float(btc_item.get("change_7d", 0))
    if delta >= 5:
        state = "STRONG"
    elif delta <= -5:
        state = "WEAK"
    else:
        state = "NEUTRAL"
    return {"delta_7d": delta, "state": state}
