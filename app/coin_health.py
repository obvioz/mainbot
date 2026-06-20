"""Health-check монеты ПЕРЕД входом — по данным биржи, не по новостям.

В отличие от news_risk.py (RSS-заголовки, ненадёжно), этот модуль смотрит на
реальное состояние торговой пары на Bybit через ccxt:

  1. Статус инструмента — пара активна и торгуется? (delist/suspend/halt → DEAD)
  2. Ликвидность — не схлопнулся ли 24h объём относительно среднего за 30 дней.
  3. Аномалия цены — монета падает одна или это общий пролив рынка (сравнение с BTC).
  4. Спред bid/ask — аномально широкий = проблема с ликвидностью.

Результат:
  health_check(coin) -> {
      "status": "HEALTHY" | "WARNING" | "ANOMALY" | "DEAD" | "UNKNOWN",
      "reasons": [...],
      "details": {...},
  }

Гейтинг (в bot.check_buy_allowed):
  DEAD / ANOMALY  → вход заблокирован
  WARNING         → вход разрешён, но с пометкой в сигнале и журнале
  HEALTHY         → вход разрешён
  UNKNOWN         → проверку не удалось выполнить (сеть/ошибка) → fail-open,
                    вход НЕ блокируем, но помечаем что здоровье не проверено.
"""

from __future__ import annotations

from app.config import normalize_coin
from app.settings import settings

# --- Пороги (в одном месте, чтобы легко тюнить) ---------------------------
# Bybit отдаёт статус инструмента; торгуется только "Trading".
TRADING_STATUSES = {"trading"}

# Объём последних суток < 20% от среднего за 30 дней → монета теряет ликвидность.
VOLUME_COLLAPSE_RATIO = 0.20

# Монета падает на >=15% за сутки, при этом BTC держится (>= -5%) → аномалия
# конкретно по этой монете, а не общий пролив рынка.
COIN_DROP_ANOMALY_PCT = -15.0
BTC_STABLE_PCT = -5.0

# Спред bid/ask шире 1.5% — признак неликвидности/проблемы.
SPREAD_WARN_PCT = 1.5


def _symbol(coin: str) -> str:
    return f"{normalize_coin(coin)}/{settings.quote}"


def _market_status(exchange, symbol: str) -> tuple[bool, str | None, dict]:
    """Вернуть (is_listed, raw_status, market_dict).

    is_listed=False означает, что пары вообще нет в списке (делистинг/нет такой пары).
    raw_status — строковый статус инструмента из info, если есть.
    """
    try:
        markets = getattr(exchange, "markets", None)
        if not markets:
            exchange.load_markets()
            markets = getattr(exchange, "markets", {}) or {}
    except Exception:
        markets = getattr(exchange, "markets", {}) or {}

    market = markets.get(symbol)
    if market is None:
        # Подстраховка для фейков/ccxt: метод market() может бросить.
        try:
            market = exchange.market(symbol)
        except Exception:
            return False, None, {}

    info = market.get("info") or {}
    raw_status = info.get("status") or info.get("Status")
    return True, (str(raw_status) if raw_status is not None else None), market


def _is_trading(active, raw_status: str | None) -> bool:
    """Пара торгуется, если ccxt active=True и статус инструмента 'Trading'.

    Если биржа не отдаёт явный статус (raw_status=None), опираемся только на active.
    """
    if active is False:
        return False
    if raw_status is not None and raw_status.strip().lower() not in TRADING_STATUSES:
        return False
    return True


def _btc_change_24h(exchange) -> float | None:
    try:
        t = exchange.fetch_ticker(f"BTC/{settings.quote}")
        pct = t.get("percentage")
        return float(pct) if pct is not None else None
    except Exception:
        return None


def _volume_ratio(exchange, symbol: str, ticker: dict) -> float | None:
    """Истинный 24h объём (rolling, из тикера) к среднему дневному за 30 завершённых дней.

    Считаем в quote-валюте, чтобы 24h-объём тикера и дневные свечи были сопоставимы.
    ВАЖНО: последнюю (текущую, неполную) дневную свечу исключаем — иначе её
    частичный объём занижал бы базу и давал ложный WARNING почти по любой монете.
    """
    # 24h объём в quote-валюте из тикера (rolling, полный).
    vol_24h = None
    qv = ticker.get("quoteVolume")
    if qv:
        try:
            vol_24h = float(qv)
        except (TypeError, ValueError):
            vol_24h = None
    if vol_24h is None:
        bv, last = ticker.get("baseVolume"), ticker.get("last")
        try:
            if bv and last:
                vol_24h = float(bv) * float(last)
        except (TypeError, ValueError):
            vol_24h = None
    if vol_24h is None:
        return None

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=32)
    except Exception:
        return None
    if not ohlcv or len(ohlcv) < 8:
        return None

    # Исключаем последнюю свечу (текущий неполный день), берём до 30 завершённых.
    days = ohlcv[:-1][-30:]
    quote_vols = []
    for row in days:
        try:
            close, vol = float(row[4]), float(row[5])
        except (TypeError, ValueError, IndexError):
            continue
        if close > 0 and vol >= 0:
            quote_vols.append(vol * close)
    if len(quote_vols) < 5:
        return None
    avg = sum(quote_vols) / len(quote_vols)
    if avg <= 0:
        return None
    return vol_24h / avg


def _spread_pct(ticker: dict) -> float | None:
    try:
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if not bid or not ask or bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        return (ask - bid) / mid * 100
    except Exception:
        return None


def health_check(coin: str, exchange=None, btc_change_24h: float | None = None) -> dict:
    """Проверить здоровье монеты по данным биржи перед входом.

    exchange — опциональный ccxt-инстанс (или duck-typed мок для тестов).
                Если None — создаётся через market.make_exchange().
    btc_change_24h — суточное изменение BTC в %, чтобы отличить аномалию монеты
                от общего пролива. Если None — берётся с биржи.
    """
    coin = normalize_coin(coin)
    symbol = _symbol(coin)
    details: dict = {"symbol": symbol}
    reasons: list[str] = []

    if exchange is None:
        try:
            from app.market import make_exchange
            exchange = make_exchange()
        except Exception as exc:
            return {
                "status": "UNKNOWN",
                "reasons": [f"не удалось подключиться к бирже: {exc}"],
                "details": details,
            }

    # --- 1. Статус торговой пары (жёсткий приоритет) ---------------------
    try:
        is_listed, raw_status, market = _market_status(exchange, symbol)
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "reasons": [f"не удалось получить статус пары: {exc}"],
            "details": details,
        }

    details["listed"] = is_listed
    details["instrument_status"] = raw_status

    if not is_listed:
        reasons.append(f"пара {symbol} отсутствует на бирже (делистинг или нет листинга)")
        return {"status": "DEAD", "reasons": reasons, "details": details}

    active = market.get("active")
    details["active"] = active
    if not _is_trading(active, raw_status):
        status_txt = raw_status or ("неактивна" if active is False else "не торгуется")
        reasons.append(f"пара {symbol} не торгуется (статус: {status_txt}) — вход запрещён")
        return {"status": "DEAD", "reasons": reasons, "details": details}

    # --- Тикер (нужен для аномалии цены и спреда) ------------------------
    ticker: dict = {}
    try:
        ticker = exchange.fetch_ticker(symbol) or {}
    except Exception as exc:
        details["ticker_error"] = str(exc)

    change_24h = ticker.get("percentage")
    if change_24h is not None:
        try:
            change_24h = float(change_24h)
        except (TypeError, ValueError):
            change_24h = None
    details["change_24h"] = change_24h

    # --- 3. Аномалия цены vs рынок (BTC) — может дать ANOMALY ------------
    status = "HEALTHY"

    if change_24h is not None and change_24h <= COIN_DROP_ANOMALY_PCT and coin != "BTC":
        if btc_change_24h is None:
            btc_change_24h = _btc_change_24h(exchange)
        details["btc_change_24h"] = btc_change_24h
        if btc_change_24h is not None and btc_change_24h >= BTC_STABLE_PCT:
            reasons.append(
                f"{coin} {change_24h:+.1f}% за сутки при BTC {btc_change_24h:+.1f}% — "
                f"падение монеты не объясняется рынком, вход заблокирован до выяснения"
            )
            status = "ANOMALY"

    # --- 2. Ликвидность: схлопывание объёма (WARNING) -------------------
    vol_ratio = _volume_ratio(exchange, symbol, ticker)
    details["volume_ratio_24h_vs_30d"] = round(vol_ratio, 3) if vol_ratio is not None else None
    if vol_ratio is not None and vol_ratio < VOLUME_COLLAPSE_RATIO:
        reasons.append(
            f"24h объём {vol_ratio * 100:.0f}% от среднего за 30д — монета теряет ликвидность"
        )
        if status == "HEALTHY":
            status = "WARNING"

    # --- 4. Спред bid/ask (WARNING) -------------------------------------
    spread = _spread_pct(ticker)
    details["spread_pct"] = round(spread, 3) if spread is not None else None
    if spread is not None and spread > SPREAD_WARN_PCT:
        reasons.append(f"широкий спред bid/ask {spread:.2f}% — низкая ликвидность")
        if status == "HEALTHY":
            status = "WARNING"

    if status == "HEALTHY" and not reasons:
        reasons.append("пара торгуется, ликвидность и спред в норме")

    return {"status": status, "reasons": reasons, "details": details}


def short_health_text(health: dict | None) -> str:
    if not health:
        return "здоровье: нет данных"
    status = health.get("status", "UNKNOWN")
    reasons = health.get("reasons") or []
    head = reasons[0] if reasons else ""
    return f"здоровье: {status} — {head}" if head else f"здоровье: {status}"


def blocks_entry(health: dict | None) -> bool:
    """DEAD/ANOMALY блокируют вход. WARNING/HEALTHY/UNKNOWN — нет."""
    if not health:
        return False
    return health.get("status") in {"DEAD", "ANOMALY"}
