"""Персональный профиль волатильности каждой монеты.

Фундамент адаптивной свинг-модели входа: вместо фиксированных уровней -15/-25/-40%
уровни DCA считаются от РЕАЛЬНОЙ волатильности каждой монеты (в единицах её ATR).

Два слоя ATR на дневных свечах:
  • Длинный (180 дней) — характер монеты → класс CALM / MEDIUM / WILD.
  • Короткий (14 дней) — текущий пульс → atr_short_pct.

Режим волатильности = короткий / длинный:
  • ratio < 0.7   → QUIET     (тише обычного, входы можно ближе)
  • 0.7 ≤ r ≤ 1.3 → NORMAL    (обычное состояние)
  • ratio > 1.3   → EXPANDING (разгоняется, осторожно, входы шире)

Персональные уровни входа — откаты от недавнего локального максимума в единицах
короткого ATR: 1.0× / 2.0× / 3.5×. CALM-монета получает узкую лесенку
(BTC ~ -3/-6/-10%), WILD — широкую (альт ~ -8/-16/-28%).

ВАЖНО: модуль ПОКА только считает и хранит профили. Интеграция в signals.py — отдельный шаг.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import COINS, normalize_coin
from app.settings import settings
from app.storage import _atomic_write_json, _read_json, now_iso

PROFILES_PATH = Path("data/volatility_profiles.json")

# Автопересчёт раз в 7 дней (как у strategy bootstrap).
REFRESH_MAX_AGE_DAYS = 7

LONG_WINDOW = 180   # длинный ATR — характер монеты
SHORT_WINDOW = 14   # короткий ATR — текущий пульс
RECENT_HIGH_WINDOW = 14  # окно недавнего локального максимума

# Множители короткого ATR для лесенки доборов.
ATR_MULTIPLES = (1.0, 2.0, 3.5)

# Пороги класса монеты по длинному ATR (%).
CALM_MAX_PCT = 3.0
MEDIUM_MAX_PCT = 6.0

# Пороги режима волатильности (короткий / длинный).
REGIME_QUIET_MAX = 0.7
REGIME_NORMAL_MAX = 1.3


# --------------------------------------------------------------------------
# Чистые расчёты (без сети — легко тестировать)
# --------------------------------------------------------------------------
def _atr_pct(highs: list[float], lows: list[float], closes: list[float], window: int) -> float | None:
    """Средний True Range за последние `window` дней, в % от текущей цены.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    Возвращает None, если данных недостаточно.
    """
    n = len(closes)
    if n < 2 or len(highs) != n or len(lows) != n:
        return None
    trs: list[float] = []
    for i in range(1, n):
        h, l, prev_close = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    if not trs:
        return None
    w = min(window, len(trs))
    atr = sum(trs[-w:]) / w
    current = closes[-1]
    if current <= 0:
        return None
    return atr / current * 100.0


def classify_coin(atr_long_pct: float) -> str:
    """Класс монеты по длинному ATR: CALM / MEDIUM / WILD."""
    if atr_long_pct < CALM_MAX_PCT:
        return "CALM"
    if atr_long_pct <= MEDIUM_MAX_PCT:
        return "MEDIUM"
    return "WILD"


def classify_regime(ratio: float) -> str:
    """Режим волатильности по ratio = короткий/длинный ATR."""
    if ratio < REGIME_QUIET_MAX:
        return "QUIET"
    if ratio <= REGIME_NORMAL_MAX:
        return "NORMAL"
    return "EXPANDING"


def entry_levels_pct(atr_short_pct: float) -> list[float]:
    """Лесенка доборов в % от локального максимума = множители короткого ATR."""
    return [round(m * atr_short_pct, 1) for m in ATR_MULTIPLES]


def compute_profile(
    coin: str,
    atr_long_pct: float,
    atr_short_pct: float,
    recent_high: float | None = None,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Собрать профиль из посчитанных ATR (чистая функция, без сети)."""
    coin = normalize_coin(coin)
    ratio = (atr_short_pct / atr_long_pct) if atr_long_pct > 0 else 0.0
    return {
        "coin": coin,
        "atr_long_pct": round(float(atr_long_pct), 2),
        "atr_short_pct": round(float(atr_short_pct), 2),
        "class": classify_coin(atr_long_pct),
        "vol_regime": classify_regime(ratio),
        "ratio": round(ratio, 2),
        "entry_levels": entry_levels_pct(atr_short_pct),
        "recent_high": round(float(recent_high), 8) if recent_high else None,
        "current_price": round(float(current_price), 8) if current_price else None,
        "updated_at": now_iso(),
    }


def build_profile_from_ohlcv(coin: str, ohlcv: list[list]) -> dict[str, Any] | None:
    """Построить профиль из сырых дневных свечей ccxt: [ts, o, h, l, c, v].

    Возвращает None, если данных слишком мало для короткого ATR.
    """
    if not ohlcv or len(ohlcv) < SHORT_WINDOW + 1:
        return None
    highs = [float(r[2]) for r in ohlcv]
    lows = [float(r[3]) for r in ohlcv]
    closes = [float(r[4]) for r in ohlcv]

    atr_long = _atr_pct(highs, lows, closes, LONG_WINDOW)
    atr_short = _atr_pct(highs, lows, closes, SHORT_WINDOW)
    if atr_long is None or atr_short is None or atr_long <= 0:
        return None

    recent_high = max(highs[-RECENT_HIGH_WINDOW:]) if highs else None
    return compute_profile(
        coin,
        atr_long_pct=atr_long,
        atr_short_pct=atr_short,
        recent_high=recent_high,
        current_price=closes[-1],
    )


# --------------------------------------------------------------------------
# Хранение
# --------------------------------------------------------------------------
def _read_store() -> dict:
    data = _read_json(PROFILES_PATH, {})
    if not isinstance(data, dict):
        return {}
    return data


def get_profile(coin: str) -> dict | None:
    """Быстрый читатель профиля одной монеты."""
    coin = normalize_coin(coin)
    return (_read_store().get("profiles") or {}).get(coin)


def all_profiles() -> dict[str, dict]:
    return dict(_read_store().get("profiles") or {})


def profiles_age_days() -> float | None:
    """Возраст файла профилей в днях по полю updated_at. None — если файла нет."""
    store = _read_store()
    ts = store.get("updated_at")
    if not ts:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 86400.0
    except Exception:
        return None


def profiles_need_refresh(max_age_days: int = REFRESH_MAX_AGE_DAYS) -> bool:
    """True, если профилей нет или они старше max_age_days."""
    age = profiles_age_days()
    return age is None or age > max_age_days


# --------------------------------------------------------------------------
# Пересчёт
# --------------------------------------------------------------------------
def _fetch_daily_ohlcv(exchange, coin: str, limit: int = 200) -> list[list]:
    symbol = f"{normalize_coin(coin)}/{settings.quote}"
    return exchange.fetch_ohlcv(symbol, timeframe="1d", limit=limit)


def refresh_all_profiles(coins: list[str] | None = None, exchange=None) -> dict[str, Any]:
    """Пересчитать профили всех (или указанных) монет и сохранить в JSON.

    Возвращает store-словарь {updated_at, profiles, errors}. Падение по одной
    монете не срывает остальной расчёт.
    """
    coins = [normalize_coin(c) for c in (coins or COINS)]
    if exchange is None:
        from app.market import make_exchange  # lazy: избегаем циклов/тяжёлого импорта
        exchange = make_exchange()

    store = _read_store()
    profiles: dict[str, dict] = dict(store.get("profiles") or {})
    errors: dict[str, str] = {}

    for coin in coins:
        try:
            ohlcv = _fetch_daily_ohlcv(exchange, coin)
            profile = build_profile_from_ohlcv(coin, ohlcv)
            if profile is None:
                errors[coin] = "недостаточно данных для расчёта ATR"
                continue
            profiles[coin] = profile
        except Exception as exc:
            errors[coin] = str(exc)

    out = {"updated_at": now_iso(), "profiles": profiles, "errors": errors}
    _atomic_write_json(PROFILES_PATH, out)
    return out


def maybe_refresh_profiles(max_age_days: int = REFRESH_MAX_AGE_DAYS) -> bool:
    """Пересчитать профили, если они устарели. Возвращает True, если пересчитал."""
    if not profiles_need_refresh(max_age_days):
        return False
    refresh_all_profiles()
    return True


# --------------------------------------------------------------------------
# Отчёт для Telegram
# --------------------------------------------------------------------------
_CLASS_ICON = {"CALM": "🟢", "MEDIUM": "🟡", "WILD": "🔴"}
_REGIME_ICON = {"QUIET": "😴", "NORMAL": "➖", "EXPANDING": "⚡"}


def format_volprofiles_table(store: dict | None = None) -> str:
    """Моноширинная таблица профилей волатильности по монетам."""
    if store is None:
        store = _read_store()
    profiles = store.get("profiles") or {}
    if not profiles:
        return "Профили волатильности ещё не посчитаны. Запусти /volprofiles для расчёта."

    updated = store.get("updated_at", "?")
    rows = sorted(profiles.values(), key=lambda p: p.get("atr_long_pct", 0))

    header = f"{'Монета':<7}{'Класс':<8}{'long':>6}{'short':>7}{'режим':>11}  уровни входа"
    lines = ["📊 Профиль волатильности монет", f"обновлено: {updated}", "", "```", header, "-" * len(header)]
    for p in rows:
        coin = p.get("coin", "?")
        cls = p.get("class", "?")
        cls_disp = f"{_CLASS_ICON.get(cls, '')}{cls}"
        long_p = p.get("atr_long_pct", 0)
        short_p = p.get("atr_short_pct", 0)
        regime = p.get("vol_regime", "?")
        regime_disp = f"{_REGIME_ICON.get(regime, '')}{regime}"
        levels = p.get("entry_levels") or []
        levels_disp = " / ".join(f"-{lvl:g}%" for lvl in levels) if levels else "—"
        lines.append(
            f"{coin:<7}{cls_disp:<8}{long_p:>5.1f}%{short_p:>6.1f}%{regime_disp:>11}  {levels_disp}"
        )
    lines.append("```")

    errors = store.get("errors") or {}
    if errors:
        lines.append("")
        lines.append("⚠️ не посчитаны: " + ", ".join(f"{c} ({e})" for c, e in errors.items()))

    lines.append("")
    lines.append("CALM <3% · MEDIUM 3–6% · WILD >6% (длинный ATR) | QUIET/NORMAL/EXPANDING = short/long")
    return "\n".join(lines)
