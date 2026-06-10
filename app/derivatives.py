from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import normalize_coin

CACHE_PATH = Path("data/derivatives_cache.json")


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


def _fresh(row: dict, minutes: int = 12) -> bool:
    try:
        return datetime.now() - datetime.fromisoformat(row.get("ts", "")) < timedelta(minutes=minutes)
    except Exception:
        return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def derivatives_symbol(exchange, coin: str) -> str | None:
    """Ищем USDT-perp символ для Bybit через ccxt.

    Обычно это BTC/USDT:USDT, ETH/USDT:USDT и т.д.
    Если ccxt/биржа поменяли формат, пытаемся найти рынок по base/quote/swap.
    """
    coin = normalize_coin(coin)
    direct = f"{coin}/USDT:USDT"
    try:
        exchange.load_markets()
        if direct in exchange.markets:
            return direct
        for symbol, market in exchange.markets.items():
            if (
                market.get("base") == coin
                and market.get("quote") == "USDT"
                and (market.get("swap") or market.get("type") == "swap")
                and market.get("linear", True)
            ):
                return symbol
    except Exception:
        pass
    return direct


def classify_funding(funding_pct: float | None) -> tuple[str, str, int]:
    """Возвращает класс, человеческий комментарий и поправку к score."""
    if funding_pct is None:
        return "UNKNOWN", "funding недоступен", 0
    if funding_pct >= 0.08:
        return "OVERHEATED", f"funding высокий {funding_pct:.3f}% — лонги перегреты", -12
    if funding_pct >= 0.04:
        return "HOT", f"funding повышен {funding_pct:.3f}% — вход осторожнее", -6
    if funding_pct <= -0.03:
        return "SHORT_PRESSURE", f"funding отрицательный {funding_pct:.3f}% — рынок напуган", 6
    return "NEUTRAL", f"funding нейтральный {funding_pct:.3f}%", 3


def classify_oi(open_interest_value: float | None, oi_change_pct: float | None) -> tuple[str, str, int]:
    if open_interest_value is None:
        return "UNKNOWN", "open interest недоступен", 0
    if oi_change_pct is None:
        return "NEUTRAL", "open interest доступен, тренд не рассчитан", 0
    if oi_change_pct >= 25:
        return "EXPANDING_FAST", f"OI резко растет {oi_change_pct:+.1f}% — риск ликвидаций", -10
    if oi_change_pct >= 12:
        return "EXPANDING", f"OI растет {oi_change_pct:+.1f}% — осторожно", -4
    if oi_change_pct <= -15:
        return "FLUSHED", f"OI снизился {oi_change_pct:+.1f}% — плечи частично смыты", 5
    return "NEUTRAL", f"OI без сильного перекоса {oi_change_pct:+.1f}%", 2


def fetch_derivatives_snapshot(exchange, coin: str) -> dict:
    """Funding + Open Interest для USDT perpetual.

    Данные публичные. Если биржа/ccxt не отдают часть метрик, бот не падает,
    а возвращает UNKNOWN, чтобы стратегия продолжала работать.
    """
    coin = normalize_coin(coin)
    cache = _read_cache()
    if coin in cache and _fresh(cache[coin], 12):
        return cache[coin]["value"]

    result = {
        "coin": coin,
        "symbol": None,
        "funding_rate": None,
        "funding_pct": None,
        "funding_state": "UNKNOWN",
        "funding_comment": "funding недоступен",
        "funding_score_adj": 0,
        "open_interest_value": None,
        "open_interest_amount": None,
        "oi_change_pct": None,
        "oi_state": "UNKNOWN",
        "oi_comment": "open interest недоступен",
        "oi_score_adj": 0,
        "source": "bybit derivatives via ccxt",
    }

    try:
        symbol = derivatives_symbol(exchange, coin)
        result["symbol"] = symbol

        # Funding rate: ccxt normalized endpoint. Bybit также отдает fundingRate в тикере perp.
        funding_rate = None
        try:
            fr = exchange.fetch_funding_rate(symbol)
            funding_rate = _safe_float(fr.get("fundingRate"), None)
        except Exception:
            try:
                ticker = exchange.fetch_ticker(symbol)
                info = ticker.get("info", {}) or {}
                funding_rate = _safe_float(info.get("fundingRate") or ticker.get("fundingRate"), None)
            except Exception:
                funding_rate = None

        if funding_rate is not None:
            funding_pct = funding_rate * 100
            state, comment, adj = classify_funding(funding_pct)
            result.update({
                "funding_rate": funding_rate,
                "funding_pct": funding_pct,
                "funding_state": state,
                "funding_comment": comment,
                "funding_score_adj": adj,
            })

        # Open Interest current + optional history trend.
        oi_value = None
        oi_amount = None
        try:
            oi = exchange.fetch_open_interest(symbol)
            oi_value = _safe_float(oi.get("openInterestValue") or oi.get("openInterestUsd"), None)
            oi_amount = _safe_float(oi.get("openInterestAmount") or oi.get("openInterest"), None)
            info = oi.get("info", {}) or {}
            if oi_value is None:
                oi_value = _safe_float(info.get("openInterestValue"), None)
            if oi_amount is None:
                oi_amount = _safe_float(info.get("openInterest"), None)
        except Exception:
            try:
                ticker = exchange.fetch_ticker(symbol)
                info = ticker.get("info", {}) or {}
                oi_value = _safe_float(info.get("openInterestValue"), None)
                oi_amount = _safe_float(info.get("openInterest"), None)
            except Exception:
                pass

        oi_change_pct = None
        try:
            hist = exchange.fetch_open_interest_history(symbol, timeframe="1d", limit=4)
            vals = []
            for row in hist or []:
                val = _safe_float(row.get("openInterestValue") or row.get("openInterestAmount") or row.get("openInterest"), None)
                if val is not None and val > 0:
                    vals.append(val)
            if len(vals) >= 2:
                oi_change_pct = (vals[-1] / vals[0] - 1) * 100
        except Exception:
            oi_change_pct = None

        if oi_value is not None or oi_amount is not None:
            state, comment, adj = classify_oi(oi_value if oi_value is not None else oi_amount, oi_change_pct)
            result.update({
                "open_interest_value": oi_value,
                "open_interest_amount": oi_amount,
                "oi_change_pct": oi_change_pct,
                "oi_state": state,
                "oi_comment": comment,
                "oi_score_adj": adj,
            })
    except Exception as exc:
        result["error"] = str(exc)

    cache[coin] = {"ts": datetime.now().isoformat(timespec="seconds"), "value": result}
    _write_cache(cache)
    return result


def short_derivatives_text(d: dict | None) -> str:
    if not d:
        return "деривативы: нет данных"
    funding = d.get("funding_comment", "funding недоступен")
    oi = d.get("oi_comment", "OI недоступен")
    return f"{funding}; {oi}"
