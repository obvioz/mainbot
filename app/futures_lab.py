from __future__ import annotations

import csv
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt

from app.settings import settings
from app.storage import load_portfolio, update_portfolio

FUTURES_KEY = "futures_lab"
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
LEVERAGE = 5
VIRTUAL_USDT_START = 1000.0
RISK_PER_TRADE = 0.02  # legacy fallback; live sizing uses get_risk_pct(score)
MAX_CONCURRENT = 1

# ─── Score-based position sizing & portfolio risk ceiling ──────────────────────
MIN_ENTRY_SCORE = 70               # signals below this are not traded at all
# Risk % of deposit by signal score. Ordered high→low; first matching threshold wins.
RISK_TIERS: list[tuple[float, float]] = [
    (85.0, 3.0),   # score 85-100 → 3% (confident)
    (75.0, 2.0),   # score 75-84  → 2% (average)
    (70.0, 1.0),   # score 70-74  → 1% (weak)
]
MAX_PORTFOLIO_RISK_PCT = 6.0       # total open risk ceiling across all positions
MIN_RISK_PCT = 0.5                 # below this remaining headroom, do not enter at all


def get_risk_pct(score: float | None) -> float:
    """Risk % of deposit for a given signal score. Below MIN_ENTRY_SCORE → 0 (no entry)."""
    if score is None or score < MIN_ENTRY_SCORE:
        return 0.0
    for threshold, pct in RISK_TIERS:
        if score >= threshold:
            return pct
    return 0.0
TIMEFRAME = "1h"
CANDLE_LIMIT = 100
ATR_PERIOD = 14
ATR_STOP_MULT = 1.5
TP_RISK_RATIO = 2.0
BREAKOUT_PERIOD = 20
MAX_JOURNAL_ROWS = 2000

# ─── Circuit breakers ──────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 6.0          # stop new entries if day loss reaches this % of day-start balance
MAX_CONSECUTIVE_LOSSES = 3          # consecutive losing trades that trigger a pause
COOLDOWN_AFTER_LOSSES_HOURS = 6     # pause length after MAX_CONSECUTIVE_LOSSES
STOP_COOLDOWN_HOURS = 2             # do not re-enter the same symbol for this long after a stop
SHORT_STOP_ATR_MULT = 1.0          # tighter stop for shorts (vs ATR_STOP_MULT for longs)
PUMP_GUARD_PCT = 5.0               # emergency-exit a short if price pumps >= this % in one 1h candle
VOL_GUARD_ATR_MULT = 2.0           # block new shorts if latest ATR > this * average ATR
REGIME_EMA_PERIOD = 50             # EMA period (1h) used for market-regime filter
REGIME_SLOPE_LOOKBACK = 3          # candles back to measure EMA50 slope

# ─── Macro trend-direction filter (4h EMA50/EMA200) ────────────────────────────
# Strict direction gate built on real futures_lab data: longs won 20% (-$43),
# shorts won 67% (+$130). We only take a side when BOTH the BTC barometer and the
# traded coin agree on a 4h trend. Sideways (FLAT) on either → stand aside.
BTC_SYMBOL = "BTC/USDT:USDT"        # market barometer
REGIME_TIMEFRAME = "4h"
REGIME_EMA_FAST = 50                # 4h fast EMA
REGIME_EMA_SLOW = 200               # 4h slow EMA (trend backbone)
REGIME_CANDLE_LIMIT = 320           # enough history for EMA200 + slope lookback
REGIME_MACRO_SLOPE_LOOKBACK = 3     # 4h candles back to measure EMA50 slope
REGIME_REFRESH_HOURS = 1            # recompute the macro regime at most once per hour

# ─── "Thinking" quality layer (three independent filters on top of everything) ──
# Each filter sits ABOVE the direction filter (BTC+coin regime) and circuit
# breakers — it never replaces them. Each is toggled independently so its
# contribution can be measured in isolation, and every block is logged with
# ``which_filter`` (mtf / confluence / structure) plus details.


def _env_flag(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on", "да"}


MTF_FILTER_ON = _env_flag("FUTURES_MTF_FILTER", True)              # Layer 1: multi-timeframe alignment
CONFLUENCE_FILTER_ON = _env_flag("FUTURES_CONFLUENCE_FILTER", True)  # Layer 2: setup confluence
STRUCTURE_FILTER_ON = _env_flag("FUTURES_STRUCTURE_FILTER", True)  # Layer 3: market structure (S/R)

MTF_TIMEFRAMES = ("1d", "4h", "1h")   # higher→lower; 1d/4h gate, 1h times the entry
MIN_CONFLUENCE = 3                    # need at least this many independent confirmations
CONFLUENCE_MIN_MAGNITUDE = 10.0       # score_components["magnitude"] ≥ this = quality pullback/breakout
CONFLUENCE_ROOM_ATR = 0.5             # entry-TF room to opposite extreme (in ATR) to count as clear
STRUCTURE_MIN_ATR = 1.0              # min distance to the opposite 4h swing level, in ATR units
STRUCTURE_SWING_TF = "4h"
STRUCTURE_SWING_LOOKBACK = 20        # 4h candles scanned for swing highs/lows
STRUCTURE_SWING_WING = 2            # bars on each side required to qualify a local extreme
THINKING_TREND_CANDLES = 320         # history per TF for EMA50/EMA200 trend classification
THINKING_VOLUME_LOOKBACK = 20        # 1h candles for the average-volume baseline


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None


def _stop_mult(side: str) -> float:
    """Asymmetric stop distance — shorts use a tighter stop (unbounded upside risk)."""
    return SHORT_STOP_ATR_MULT if side == "short" else ATR_STOP_MULT


def _classify_regime(price: float, ema50: float, ema50_prev: float) -> str:
    """uptrend / downtrend / range based on price vs EMA50(1h) and EMA50 slope."""
    if any(math.isnan(x) for x in (price, ema50, ema50_prev)):
        return "range"
    slope = ema50 - ema50_prev
    if price < ema50 and slope < 0:
        return "downtrend"
    if price > ema50 and slope > 0:
        return "uptrend"
    return "range"


def _entry_allowed_by_regime(side: str, regime: str) -> bool:
    """Symmetric trend filter: no longs into a downtrend, no shorts into an uptrend."""
    if side == "long" and regime == "downtrend":
        return False
    if side == "short" and regime == "uptrend":
        return False
    return True


# ─── Macro 4h direction filter (UP / DOWN / FLAT) ──────────────────────────────

def _classify_macro_regime(
    price: float, ema50: float, ema50_prev: float, ema200: float
) -> str:
    """4h trend direction from price vs EMA200, EMA50 slope, and EMA50 vs EMA200.

    UP:   price > EMA200 AND EMA50 rising AND EMA50 > EMA200
    DOWN: price < EMA200 AND EMA50 falling AND EMA50 < EMA200
    FLAT: anything mixed (stand aside).
    """
    if any(math.isnan(x) for x in (price, ema50, ema50_prev, ema200)):
        return "FLAT"
    slope = ema50 - ema50_prev
    if price > ema200 and slope > 0 and ema50 > ema200:
        return "UP"
    if price < ema200 and slope < 0 and ema50 < ema200:
        return "DOWN"
    return "FLAT"


def _direction_filter_block(side: str, btc_regime: str, coin_regime: str) -> str | None:
    """Double trend filter. Returns a block reason, or None if the entry is allowed.

    LONG only when btc == UP AND coin == UP; SHORT only when btc == DOWN AND coin == DOWN.
    Reasons:
      flat_market          — BTC barometer is sideways (whole market FLAT)
      btc_coin_divergence  — BTC trends but the coin disagrees (opposite or FLAT)
      regime_mismatch      — both agree on a trend, but it's against the trade side
    """
    want = "UP" if side == "long" else "DOWN"
    if btc_regime == want and coin_regime == want:
        return None
    if btc_regime == "FLAT":
        return "flat_market"
    if coin_regime == "FLAT" or btc_regime != coin_regime:
        return "btc_coin_divergence"
    return "regime_mismatch"


def _allowed_now(btc_regime: str, coin_regime: str) -> str:
    """Human-readable summary of what the direction filter permits for a coin."""
    if _direction_filter_block("long", btc_regime, coin_regime) is None:
        return "лонги"
    if _direction_filter_block("short", btc_regime, coin_regime) is None:
        return "шорты"
    return "ничего"


def market_regime_analysis(exchange, symbol: str) -> dict[str, Any]:
    """Compute the 4h macro regime (UP/DOWN/FLAT) for a symbol via EMA50/EMA200."""
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe=REGIME_TIMEFRAME, limit=REGIME_CANDLE_LIMIT)
    except Exception as exc:
        return {"symbol": symbol, "regime": "FLAT", "error": str(exc)}

    if not candles or len(candles) < REGIME_EMA_SLOW + REGIME_MACRO_SLOPE_LOOKBACK + 1:
        return {"symbol": symbol, "regime": "FLAT", "error": "insufficient_data"}

    closes = [float(c[4]) for c in candles]
    price = closes[-1]
    ema_fast = _compute_ema(closes, REGIME_EMA_FAST)
    ema_slow = _compute_ema(closes, REGIME_EMA_SLOW)

    ema50 = ema_fast[-1]
    ema50_prev = ema_fast[-1 - REGIME_MACRO_SLOPE_LOOKBACK]
    ema200 = ema_slow[-1]
    regime = _classify_macro_regime(price, ema50, ema50_prev, ema200)

    return {
        "symbol": symbol,
        "regime": regime,
        "price": round(price, 4),
        "ema50": round(ema50, 4) if not math.isnan(ema50) else None,
        "ema200": round(ema200, 4) if not math.isnan(ema200) else None,
        "ema50_slope": round(ema50 - ema50_prev, 6) if not math.isnan(ema50 - ema50_prev) else None,
    }


def _get_macro_regimes(
    exchange, lab: dict[str, Any], now: datetime, force: bool = False
) -> dict[str, Any]:
    """Hourly-cached macro regimes for BTC + every traded symbol.

    Recomputes at most once per REGIME_REFRESH_HOURS (cache key: last_regime_update).
    """
    cache = lab.get("macro_regime") or {}
    last = _parse_iso(lab.get("last_regime_update"))
    fresh = (
        last is not None
        and (now - last) < timedelta(hours=REGIME_REFRESH_HOURS)
        and bool(cache.get("coins"))
    )
    if fresh and not force:
        return cache

    coins: dict[str, str] = {}
    for sym in SYMBOLS:
        coins[sym] = market_regime_analysis(exchange, sym).get("regime", "FLAT")
    btc = coins.get(BTC_SYMBOL)
    if btc is None:
        btc = market_regime_analysis(exchange, BTC_SYMBOL).get("regime", "FLAT")

    cache = {"btc": btc, "coins": coins}
    lab["macro_regime"] = cache
    lab["last_regime_update"] = now.isoformat()
    return cache


# ─── Thinking layer — pure decision helpers (no I/O; unit-tested directly) ──────

def _trend_from_closes(closes: list[float]) -> str:
    """UP/DOWN/FLAT for a timeframe from its closes via EMA50/EMA200 (same rule as 4h)."""
    if len(closes) < REGIME_EMA_SLOW + REGIME_MACRO_SLOPE_LOOKBACK + 1:
        return "FLAT"
    ema_fast = _compute_ema(closes, REGIME_EMA_FAST)
    ema_slow = _compute_ema(closes, REGIME_EMA_SLOW)
    price = closes[-1]
    ema50 = ema_fast[-1]
    ema50_prev = ema_fast[-1 - REGIME_MACRO_SLOPE_LOOKBACK]
    ema200 = ema_slow[-1]
    return _classify_macro_regime(price, ema50, ema50_prev, ema200)


def _mtf_filter_block(side: str, trend_1d: str, trend_4h: str) -> str | None:
    """LAYER 1. Higher timeframes (1d, 4h) must not be against the trade side.

    LONG needs 1d and 4h both not DOWN; SHORT needs both not UP. A higher-TF that
    opposes the side (or a 1d-up/4h-down style conflict) → block "mtf_conflict".
    The 1h timeframe is where the entry is actually timed, so it is journaled but
    does not gate here.
    """
    against = "DOWN" if side == "long" else "UP"
    if trend_1d == against or trend_4h == against:
        return "mtf_conflict"
    return None


def _confluence_factors(
    side: str,
    *,
    magnitude: float,
    vol_ratio: float,
    current: float,
    atr: float,
    high20: float | None,
    low20: float | None,
    trend_4h: str,
    atr_extreme: bool,
) -> dict[str, bool]:
    """LAYER 2 inputs. Five INDEPENDENT confirmations, each a boolean."""
    want = "UP" if side == "long" else "DOWN"
    factors: dict[str, bool] = {}
    # 1) higher-TF trend agrees with the side
    factors["htf_trend"] = trend_4h == want
    # 2) the pullback/breakout on the entry TF is deep/clean enough
    factors["setup_quality"] = float(magnitude or 0) >= CONFLUENCE_MIN_MAGNITUDE
    # 3) volume confirms the move
    factors["volume"] = float(vol_ratio or 0) >= 1.0
    # 4) not entering right into the opposite recent extreme (entry-TF 20-bar range)
    if atr and atr > 0 and high20 is not None and low20 is not None:
        gap = (high20 - current) / atr if side == "long" else (current - low20) / atr
        factors["room_to_extreme"] = not (0 <= gap < CONFLUENCE_ROOM_ATR)
    else:
        factors["room_to_extreme"] = False
    # 5) volatility in a normal regime (not an EXPANDING/chaotic ATR)
    factors["vol_normal"] = not bool(atr_extreme)
    return factors


def _confluence_block(factors: dict[str, bool]) -> tuple[str | None, int, list[str]]:
    """LAYER 2. Fewer than MIN_CONFLUENCE confirmations → block "low_confluence"."""
    active = [k for k, v in factors.items() if v]
    score = len(active)
    if score < MIN_CONFLUENCE:
        return "low_confluence", score, active
    return None, score, active


def _swing_points(
    highs: list[float], lows: list[float], wing: int, lookback: int
) -> tuple[list[float], list[float]]:
    """Local swing highs/lows over the last ``lookback`` bars.

    A bar is a swing high if its high is the max within ``wing`` bars on each side
    (mirror for swing low). Returns the extreme prices (not indices).
    """
    n = len(highs)
    if n == 0:
        return [], []
    start = max(wing, n - lookback)
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(start, n - wing):
        window_h = highs[i - wing:i + wing + 1]
        window_l = lows[i - wing:i + wing + 1]
        if highs[i] >= max(window_h):
            swing_highs.append(highs[i])
        if lows[i] <= min(window_l):
            swing_lows.append(lows[i])
    return swing_highs, swing_lows


def _nearest_levels(
    current: float, swing_highs: list[float], swing_lows: list[float]
) -> tuple[float | None, float | None]:
    """Nearest swing high above (resistance) and swing low below (support)."""
    above = [h for h in swing_highs if h > current]
    below = [l for l in swing_lows if l < current]
    nearest_resistance = min(above) if above else None
    nearest_support = max(below) if below else None
    return nearest_resistance, nearest_support


def _structure_block(
    side: str,
    current: float,
    atr_4h: float,
    nearest_resistance: float | None,
    nearest_support: float | None,
) -> tuple[str | None, float | None]:
    """LAYER 3. Block a LONG entering just under resistance (or SHORT just over
    support) — < STRUCTURE_MIN_ATR to the opposite level is a poor risk/reward.

    Returns (block_reason|None, distance_to_level_atr).
    """
    if not atr_4h or atr_4h <= 0:
        return None, None
    if side == "long":
        if nearest_resistance is None:
            return None, None
        dist = round((nearest_resistance - current) / atr_4h, 3)
    else:
        if nearest_support is None:
            return None, None
        dist = round((current - nearest_support) / atr_4h, 3)
    if 0 <= dist < STRUCTURE_MIN_ATR:
        return "poor_structure_rr", dist
    return None, dist


def _volume_ratio(volumes: list[float], lookback: int = THINKING_VOLUME_LOOKBACK) -> float:
    """Latest volume vs the average of the preceding ``lookback`` bars."""
    if len(volumes) < lookback + 1:
        return 0.0
    baseline = volumes[-lookback - 1:-1]
    avg = sum(baseline) / len(baseline) if baseline else 0.0
    if avg <= 0:
        return 0.0
    return volumes[-1] / avg


# ─── Thinking layer — I/O orchestration ────────────────────────────────────────

def _thinking_analysis(exchange, symbol: str) -> dict[str, Any]:
    """Fetch the multi-timeframe context the three filters need (1d/4h/1h)."""
    d_candles = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=THINKING_TREND_CANDLES)
    trend_1d = _trend_from_closes([float(c[4]) for c in (d_candles or [])])

    h1_candles = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=max(THINKING_TREND_CANDLES, 60))
    trend_1h = _trend_from_closes([float(c[4]) for c in (h1_candles or [])])
    vol_ratio = _volume_ratio([float(c[5]) for c in (h1_candles or [])])

    h4_candles = exchange.fetch_ohlcv(symbol, timeframe=STRUCTURE_SWING_TF, limit=STRUCTURE_SWING_LOOKBACK * 3)
    highs = [float(c[2]) for c in (h4_candles or [])]
    lows = [float(c[3]) for c in (h4_candles or [])]
    swing_highs, swing_lows = _swing_points(highs, lows, STRUCTURE_SWING_WING, STRUCTURE_SWING_LOOKBACK)
    atr_4h_list = _compute_atr(h4_candles, ATR_PERIOD) if h4_candles else []
    atr_4h = next((a for a in reversed(atr_4h_list) if not math.isnan(a) and a > 0), 0.0)

    return {
        "trend_1d": trend_1d,
        "trend_1h": trend_1h,
        "vol_ratio": round(vol_ratio, 3),
        "atr_4h": atr_4h,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
    }


def _apply_thinking_filters(
    exchange, sig: dict[str, Any], trend_4h: str, cache: dict[str, dict]
) -> tuple[str | None, str | None, dict[str, Any], dict[str, Any]]:
    """Run the three quality filters in order (MTF → confluence → structure).

    Returns (block_reason|None, which_filter|None, block_details, assessment).
    ``assessment`` always carries every layer's measurement (even for disabled
    layers) so the entry journal can correlate each layer with the outcome.
    """
    if not (MTF_FILTER_ON or CONFLUENCE_FILTER_ON or STRUCTURE_FILTER_ON):
        return None, None, {}, {}  # all off → behave exactly as before

    symbol = sig["symbol"]
    side = sig["side"]
    try:
        ta = cache.get(symbol) or _thinking_analysis(exchange, symbol)
        cache[symbol] = ta
    except Exception as exc:
        # Fail-open: a data hiccup must not block every entry.
        return None, None, {"error": str(exc)}, {"error": str(exc)}

    trend_1d, trend_1h = ta["trend_1d"], ta["trend_1h"]
    assessment: dict[str, Any] = {
        "trend_1d": trend_1d,
        "trend_4h": trend_4h,
        "trend_1h": trend_1h,
    }

    # Layer 1 — multi-timeframe alignment
    if MTF_FILTER_ON:
        r = _mtf_filter_block(side, trend_1d, trend_4h)
        if r:
            return r, "mtf", {"trend_1d": trend_1d, "trend_4h": trend_4h, "trend_1h": trend_1h}, assessment

    # Layer 2 — setup confluence (always measured; only blocks when enabled)
    factors = _confluence_factors(
        side,
        magnitude=(sig.get("score_components") or {}).get("magnitude", 0.0),
        vol_ratio=ta["vol_ratio"],
        current=float(sig["current"]),
        atr=float(sig.get("atr") or 0),
        high20=sig.get("high20"),
        low20=sig.get("low20"),
        trend_4h=trend_4h,
        atr_extreme=bool(sig.get("atr_extreme", False)),
    )
    conf_reason, conf_score, active = _confluence_block(factors)
    assessment["confluence_score"] = conf_score
    assessment["confluence_factors"] = active
    if CONFLUENCE_FILTER_ON and conf_reason:
        return conf_reason, "confluence", {"confluence_score": conf_score, "factors": active}, assessment

    # Layer 3 — market structure (support/resistance risk-reward)
    nearest_resistance, nearest_support = _nearest_levels(
        float(sig["current"]), ta["swing_highs"], ta["swing_lows"]
    )
    struct_reason, dist_atr = _structure_block(
        side, float(sig["current"]), ta["atr_4h"], nearest_resistance, nearest_support
    )
    assessment["nearest_resistance"] = nearest_resistance
    assessment["nearest_support"] = nearest_support
    assessment["distance_to_level_atr"] = dist_atr
    if STRUCTURE_FILTER_ON and struct_reason:
        return (
            struct_reason,
            "structure",
            {
                "nearest_resistance": nearest_resistance,
                "nearest_support": nearest_support,
                "distance_to_level_atr": dist_atr,
            },
            assessment,
        )

    return None, None, {}, assessment


def _short_pump_pct(candles: list, current_price: float) -> float:
    """Max single-1h-candle upside move (open→high / open→current), used for pump guard."""
    moves: list[float] = []
    try:
        # In-progress candle: open → current live price
        cur_open = float(candles[-1][1])
        if cur_open > 0:
            moves.append((current_price - cur_open) / cur_open * 100.0)
            moves.append((float(candles[-1][2]) - cur_open) / cur_open * 100.0)
        # Last closed candle: open → high
        prev_open = float(candles[-2][1])
        if prev_open > 0:
            moves.append((float(candles[-2][2]) - prev_open) / prev_open * 100.0)
    except (IndexError, ValueError, TypeError):
        return 0.0
    return max(moves) if moves else 0.0


def _current_day_loss_pct(lab: dict[str, Any]) -> float:
    start = float(lab.get("day_start_balance") or 0)
    cur = float(lab.get("virtual_usdt") or 0)
    if start <= 0:
        return 0.0
    return max(0.0, (start - cur) / start * 100.0)


def _roll_day(lab: dict[str, Any]) -> dict[str, Any]:
    """Reset the daily-loss baseline at the start of a new UTC day."""
    today = _today_utc()
    if lab.get("day_start_date") != today:
        lab["day_start_date"] = today
        lab["day_start_balance"] = float(lab.get("virtual_usdt") or VIRTUAL_USDT_START)
        lab["daily_limit_notified_date"] = None
    return lab


def _log_blocked_entry(
    lab: dict[str, Any],
    symbol: str,
    side: str,
    strategy: str,
    regime: str,
    reason: str,
    which_filter: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    lab.setdefault("blocked_entries", [])
    row: dict[str, Any] = {
        "time": now_iso(),
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "regime": regime,
        "reason": reason,
    }
    # which_filter distinguishes the thinking layer (mtf/confluence/structure) from
    # legacy blocks so /thinking can attribute how much each layer filters out.
    if which_filter:
        row["which_filter"] = which_filter
    if details:
        row["details"] = details
    lab["blocked_entries"].append(row)
    lab["blocked_entries"] = lab["blocked_entries"][-MAX_JOURNAL_ROWS:]


def _global_entry_block(lab: dict[str, Any], now: datetime) -> str | None:
    """Account-wide entry blocks (apply to every signal, both sides)."""
    if _current_day_loss_pct(lab) >= DAILY_LOSS_LIMIT_PCT:
        return "daily_loss_limit"
    until = _parse_iso(lab.get("losses_cooldown_until"))
    if until and now < until:
        return "consecutive_loss_cooldown"
    return None


def _signal_entry_block(lab: dict[str, Any], signal: dict[str, Any], now: datetime) -> str | None:
    """Per-signal entry blocks: trend regime, short volatility guard, per-symbol stop cooldown."""
    side = signal["side"]
    symbol = signal["symbol"]
    regime = signal.get("regime", "range")

    if not _entry_allowed_by_regime(side, regime):
        return f"regime_{regime}"

    if side == "short" and signal.get("atr_extreme"):
        return "extreme_volatility"

    until = _parse_iso((lab.get("stop_cooldowns") or {}).get(symbol))
    if until and now < until:
        return "stop_cooldown"

    return None


def make_futures_exchange():
    exchange_cls = getattr(ccxt, settings.exchange_id)
    return exchange_cls({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "linear",
            "adjustForTimeDifference": True,
            "recvWindow": settings.bybit_recv_window,
        },
    })


def _compute_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [float("nan")] * len(values)
    k = 2.0 / (period + 1)
    emas = [float("nan")] * len(values)
    emas[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        emas[i] = values[i] * k + emas[i - 1] * (1 - k)
    return emas


def _compute_atr(candles: list, period: int = ATR_PERIOD) -> list[float]:
    atrs = [float("nan")] * len(candles)
    if len(candles) < period + 1:
        return atrs

    trs: list[float] = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if len(trs) < period:
        return atrs

    atrs[period] = sum(trs[:period]) / period
    for i in range(period + 1, len(candles)):
        atrs[i] = (atrs[i - 1] * (period - 1) + trs[i - 1]) / period

    return atrs


def _liquidation_price(entry: float, side: str, leverage: int) -> float:
    if side == "long":
        return entry * (1.0 - 1.0 / leverage)
    return entry * (1.0 + 1.0 / leverage)


def _stop_is_safe(entry: float, stop: float, side: str, leverage: int) -> bool:
    liq = _liquidation_price(entry, side, leverage)
    if side == "long":
        return liq < stop < entry
    return entry < stop < liq


def _calc_position_size(deposit: float, entry: float, stop: float, risk_pct: float) -> float:
    """Notional size so that hitting the stop loses risk_pct (%) of deposit."""
    if entry <= 0:
        return 0.0
    stop_distance_pct = abs(entry - stop) / entry
    if stop_distance_pct <= 0:
        return 0.0
    return (deposit * (risk_pct / 100.0)) / stop_distance_pct


def _position_risk_pct(trade: dict[str, Any], deposit: float) -> float:
    """% of deposit lost if this position's protective stop is hit."""
    entry = float(trade.get("entry_price") or 0)
    stop = float(trade.get("trailing_stop") or trade.get("stop_price") or 0)
    pos = float(trade.get("position_size_usdt") or 0)
    if entry <= 0 or deposit <= 0 or stop <= 0:
        return 0.0
    stop_distance_pct = abs(entry - stop) / entry
    return pos * stop_distance_pct / deposit * 100.0


def _open_positions(lab: dict[str, Any]) -> list[dict[str, Any]]:
    """All currently open positions. Generalized for MAX_CONCURRENT > 1; today ≤ 1."""
    positions: list[dict[str, Any]] = []
    active = lab.get("active_trade")
    if active:
        positions.append(active)
    positions.extend(lab.get("open_positions") or [])
    return positions


def _current_portfolio_risk_pct(lab: dict[str, Any]) -> float:
    deposit = float(lab.get("virtual_usdt") or 0)
    return sum(_position_risk_pct(t, deposit) for t in _open_positions(lab))


def _resolve_entry_risk(score: float, portfolio_risk_before: float) -> tuple[float, float, bool, str | None]:
    """Decide actual risk for a new entry under the portfolio ceiling.

    Returns (actual_risk_pct, assigned_risk_pct, was_trimmed, blocked_reason).
    """
    assigned = get_risk_pct(score)
    if assigned <= 0:
        return 0.0, 0.0, False, "score_below_min"
    remaining = MAX_PORTFOLIO_RISK_PCT - portfolio_risk_before
    if assigned <= remaining:
        return assigned, assigned, False, None
    if remaining >= MIN_RISK_PCT:
        return round(remaining, 4), assigned, True, None
    return 0.0, assigned, False, "portfolio_risk_ceiling"


def default_futures_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "virtual_usdt": VIRTUAL_USDT_START,
        "initial_usdt": VIRTUAL_USDT_START,
        "active_trade": None,
        "journal": [],
        "last_tick": None,
        "last_event": None,
        # Circuit-breaker state
        "day_start_date": _today_utc(),
        "day_start_balance": VIRTUAL_USDT_START,
        "daily_limit_notified_date": None,
        "consecutive_losses": 0,
        "losses_cooldown_until": None,
        "stop_cooldowns": {},
        "blocked_entries": [],
        # Macro 4h direction filter cache (refreshed hourly)
        "macro_regime": {},
        "last_regime_update": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def load_futures_state() -> dict[str, Any]:
    state = load_portfolio()
    lab = state.get(FUTURES_KEY)

    if not lab:
        lab = default_futures_state()
        # Only touch our own key so we never clobber positions/rotation_lab.
        update_portfolio(lambda s: s.update({FUTURES_KEY: lab}))
        return lab

    lab.setdefault("enabled", True)
    lab.setdefault("virtual_usdt", VIRTUAL_USDT_START)
    lab.setdefault("initial_usdt", VIRTUAL_USDT_START)
    lab.setdefault("active_trade", None)
    lab.setdefault("journal", [])
    lab.setdefault("last_tick", None)
    lab.setdefault("last_event", None)
    lab.setdefault("day_start_date", _today_utc())
    lab.setdefault("day_start_balance", float(lab.get("virtual_usdt") or VIRTUAL_USDT_START))
    lab.setdefault("daily_limit_notified_date", None)
    lab.setdefault("consecutive_losses", 0)
    lab.setdefault("losses_cooldown_until", None)
    lab.setdefault("stop_cooldowns", {})
    lab.setdefault("blocked_entries", [])
    lab.setdefault("macro_regime", {})
    lab.setdefault("last_regime_update", None)

    return lab


def save_futures_state(lab: dict[str, Any]) -> None:
    lab["updated_at"] = now_iso()
    # Atomic, key-scoped write: re-reads fresh state under the portfolio lock and
    # only replaces FUTURES_KEY, preserving positions and other labs' keys.
    update_portfolio(lambda s: s.update({FUTURES_KEY: lab}))


def _append_journal(lab: dict[str, Any], row: dict[str, Any]) -> None:
    lab.setdefault("journal", [])
    lab["journal"].append(row)
    lab["journal"] = lab["journal"][-MAX_JOURNAL_ROWS:]
    lab["last_event"] = row


# ─── Signal analysis ─────────────────────────────────────────────────────────

def _analyze_symbol(exchange, symbol: str) -> dict[str, Any] | None:
    candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
    if not candles or len(candles) < ATR_PERIOD + BREAKOUT_PERIOD + 5:
        return None

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    current = closes[-1]

    ema9_list = _compute_ema(closes, 9)
    ema30_list = _compute_ema(closes, 30)
    ema50_list = _compute_ema(closes, REGIME_EMA_PERIOD)
    atrs = _compute_atr(candles, ATR_PERIOD)

    ema9 = ema9_list[-1]
    ema30 = ema30_list[-1]
    atr = atrs[-1]

    if any(math.isnan(x) for x in [ema9, ema30, atr]) or atr <= 0 or ema30 <= 0:
        return None

    # Exclude the last (current) candle from the breakout range
    high20 = max(highs[-BREAKOUT_PERIOD - 1:-1])
    low20 = min(lows[-BREAKOUT_PERIOD - 1:-1])

    # Market regime via EMA50(1h) level + slope
    ema50 = ema50_list[-1]
    ema50_prev = (
        ema50_list[-1 - REGIME_SLOPE_LOOKBACK]
        if len(ema50_list) > REGIME_SLOPE_LOOKBACK else float("nan")
    )
    regime = _classify_regime(current, ema50, ema50_prev)

    # Extreme volatility: latest ATR vs average ATR over the window
    valid_atrs = [a for a in atrs if not math.isnan(a) and a > 0]
    atr_avg = sum(valid_atrs) / len(valid_atrs) if valid_atrs else atr
    atr_extreme = atr > VOL_GUARD_ATR_MULT * atr_avg if atr_avg > 0 else False

    return {
        "symbol": symbol,
        "current": current,
        "ema9": ema9,
        "ema30": ema30,
        "ema50": ema50,
        "atr": atr,
        "atr_avg": atr_avg,
        "atr_extreme": atr_extreme,
        "regime": regime,
        "high20": high20,
        "low20": low20,
    }


# ─── Signal scoring (0-100) ────────────────────────────────────────────────────
# Each strategy scores its own setup — the four components (trend / magnitude /
# regime / volatility) mean different things per strategy, so they are NOT shared.
# Components are logged individually (score_components) so factor attribution is
# possible offline: which factors actually predict profitable trades.

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _vol_component(atr: float, atr_avg: float, max_pts: float = 15.0) -> float:
    """Calm volatility scores high; expansion toward 2x average decays to 0."""
    if atr_avg <= 0:
        return round(max_pts * 0.5, 2)
    ratio = atr / atr_avg
    return round(_clamp(2.0 - ratio, 0.0, 1.0) * max_pts, 2)


def _regime_alignment(side: str, regime: str) -> str:
    if regime == "range":
        return "range"
    aligned = (side == "long" and regime == "uptrend") or (side == "short" and regime == "downtrend")
    return "aligned" if aligned else "opposed"


def _score_trend_pullback(data: dict[str, Any], side: str) -> tuple[float, dict[str, float]]:
    """Trend-following: reward strong trend, clean pullback depth, aligned regime, calm vol."""
    ema9, ema30, atr, current = data["ema9"], data["ema30"], data["atr"], data["current"]
    atr_avg = float(data.get("atr_avg") or atr)
    regime = data.get("regime", "range")

    # trend (0-30): EMA9/EMA30 separation in ATR units; ~1.5 ATR gap = full strength
    gap_atr = abs(ema9 - ema30) / atr if atr > 0 else 0.0
    trend = round(_clamp(gap_atr / 1.5, 0.0, 1.0) * 30.0, 2)

    # magnitude (0-30): pullback depth within the EMA band (toward EMA30 = deeper = better entry)
    band = abs(ema9 - ema30)
    depth = ((ema9 - current) if side == "long" else (current - ema9)) / band if band > 0 else 0.0
    magnitude = round(_clamp(depth, 0.0, 1.0) * 30.0, 2)

    align = _regime_alignment(side, regime)
    regime_pts = {"aligned": 25.0, "range": 12.0, "opposed": 0.0}[align]
    volatility = _vol_component(atr, atr_avg)

    components = {"trend": trend, "magnitude": magnitude, "regime": regime_pts, "volatility": volatility}
    score = round(_clamp(trend + magnitude + regime_pts + volatility, 0.0, 100.0), 1)
    return score, components


def _score_mean_reversion(data: dict[str, Any], side: str) -> tuple[float, dict[str, float]]:
    """Counter-trend: reward big deviation, calm/flat trend, range regime, calm vol."""
    ema9, ema30, atr, current = data["ema9"], data["ema30"], data["atr"], data["current"]
    atr_avg = float(data.get("atr_avg") or atr)
    regime = data.get("regime", "range")

    # trend (0-25): mean reversion prefers a FLAT trend → small EMA separation scores high
    gap_atr = abs(ema9 - ema30) / atr if atr > 0 else 0.0
    trend = round(_clamp(1.0 - gap_atr / 1.5, 0.0, 1.0) * 25.0, 2)

    # magnitude (0-35): deviation beyond 2 ATR; 2 ATR → 10pts, 4 ATR → 35pts
    dev_atr = abs(current - ema30) / atr if atr > 0 else 0.0
    magnitude = round(_clamp((dev_atr - 2.0) / 2.0, 0.0, 1.0) * 25.0 + 10.0, 2) if dev_atr >= 2.0 else 0.0

    # regime (0-25): range is ideal for reversion; an active trend is risky to fade
    regime_pts = 25.0 if regime == "range" else 8.0
    volatility = _vol_component(atr, atr_avg)

    components = {"trend": trend, "magnitude": magnitude, "regime": regime_pts, "volatility": volatility}
    score = round(_clamp(trend + magnitude + regime_pts + volatility, 0.0, 100.0), 1)
    return score, components


def _score_breakout(data: dict[str, Any], side: str) -> tuple[float, dict[str, float]]:
    """Momentum: reward decisive break distance, EMA/trend alignment, aligned regime, vol."""
    ema9, ema30, atr, current = data["ema9"], data["ema30"], data["atr"], data["current"]
    atr_avg = float(data.get("atr_avg") or atr)
    regime = data.get("regime", "range")
    high20, low20 = data["high20"], data["low20"]

    # magnitude (0-30): how far beyond the level, in ATR units; 1 ATR clear = full
    if side == "long":
        dist_atr = (current - high20) / atr if atr > 0 else 0.0
    else:
        dist_atr = (low20 - current) / atr if atr > 0 else 0.0
    magnitude = round(_clamp(dist_atr, 0.0, 1.0) * 30.0, 2)

    # trend (0-30): EMA alignment with the breakout direction; misaligned = small floor
    gap_atr = abs(ema9 - ema30) / atr if atr > 0 else 0.0
    aligned = (side == "long" and ema9 > ema30) or (side == "short" and ema9 < ema30)
    trend = round(_clamp(gap_atr / 1.5, 0.0, 1.0) * 30.0, 2) if aligned else 5.0

    align = _regime_alignment(side, regime)
    regime_pts = {"aligned": 25.0, "range": 12.0, "opposed": 0.0}[align]
    volatility = _vol_component(atr, atr_avg)

    components = {"trend": trend, "magnitude": magnitude, "regime": regime_pts, "volatility": volatility}
    score = round(_clamp(trend + magnitude + regime_pts + volatility, 0.0, 100.0), 1)
    return score, components


def _signal_trend_pullback(data: dict[str, Any]) -> dict[str, Any] | None:
    """Long in uptrend on pullback; short in downtrend on bounce."""
    current = data["current"]
    ema9 = data["ema9"]
    ema30 = data["ema30"]
    atr = data["atr"]

    # LONG: uptrend (EMA9 > EMA30), price pulled back below EMA9 but still above EMA30
    if ema9 > ema30 and ema30 < current < ema9:
        stop = current - ATR_STOP_MULT * atr
        if _stop_is_safe(current, stop, "long", LEVERAGE):
            score, components = _score_trend_pullback(data, "long")
            return {
                "side": "long",
                "stop_price": stop,
                "score": score,
                "score_components": components,
                "entry_reason": {
                    "ema9": round(ema9, 4),
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "condition": "uptrend pullback: EMA30 < price < EMA9",
                },
            }

    # SHORT: downtrend (EMA9 < EMA30), price bounced above EMA9 but still below EMA30
    if ema9 < ema30 and ema9 < current < ema30:
        stop = current + _stop_mult("short") * atr
        if _stop_is_safe(current, stop, "short", LEVERAGE):
            score, components = _score_trend_pullback(data, "short")
            return {
                "side": "short",
                "stop_price": stop,
                "score": score,
                "score_components": components,
                "entry_reason": {
                    "ema9": round(ema9, 4),
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "condition": "downtrend bounce: EMA9 < price < EMA30",
                },
            }

    return None


def _signal_mean_reversion(data: dict[str, Any]) -> dict[str, Any] | None:
    """Long when price deviates >2*ATR below EMA30; short when >2*ATR above."""
    current = data["current"]
    ema30 = data["ema30"]
    atr = data["atr"]
    deviation = current - ema30

    if deviation < -2.0 * atr:
        stop = current - ATR_STOP_MULT * atr
        if _stop_is_safe(current, stop, "long", LEVERAGE):
            score, components = _score_mean_reversion(data, "long")
            return {
                "side": "long",
                "stop_price": stop,
                "score": score,
                "score_components": components,
                "entry_reason": {
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "deviation": round(deviation, 4),
                    "deviation_atr_mult": round(deviation / atr, 2),
                    "condition": "mean_reversion: price < EMA30 - 2*ATR",
                },
            }

    if deviation > 2.0 * atr:
        stop = current + _stop_mult("short") * atr
        if _stop_is_safe(current, stop, "short", LEVERAGE):
            score, components = _score_mean_reversion(data, "short")
            return {
                "side": "short",
                "stop_price": stop,
                "score": score,
                "score_components": components,
                "entry_reason": {
                    "ema30": round(ema30, 4),
                    "atr": round(atr, 4),
                    "deviation": round(deviation, 4),
                    "deviation_atr_mult": round(deviation / atr, 2),
                    "condition": "mean_reversion: price > EMA30 + 2*ATR",
                },
            }

    return None


def _signal_breakout(data: dict[str, Any]) -> dict[str, Any] | None:
    """Long on break above 20-candle high; short on break below 20-candle low."""
    current = data["current"]
    atr = data["atr"]
    high20 = data["high20"]
    low20 = data["low20"]

    if current > high20:
        stop = high20 - 0.5 * atr
        if _stop_is_safe(current, stop, "long", LEVERAGE):
            score, components = _score_breakout(data, "long")
            return {
                "side": "long",
                "stop_price": stop,
                "score": score,
                "score_components": components,
                "entry_reason": {
                    "high20": round(high20, 4),
                    "atr": round(atr, 4),
                    "breakout_pct": round((current / high20 - 1) * 100, 3),
                    "condition": f"breakout long: {current:.2f} > high20 {high20:.2f}",
                },
            }

    if current < low20:
        stop = low20 + 0.5 * atr
        if _stop_is_safe(current, stop, "short", LEVERAGE):
            score, components = _score_breakout(data, "short")
            return {
                "side": "short",
                "stop_price": stop,
                "score": score,
                "score_components": components,
                "entry_reason": {
                    "low20": round(low20, 4),
                    "atr": round(atr, 4),
                    "breakout_pct": round((1 - current / low20) * 100, 3),
                    "condition": f"breakdown short: {current:.2f} < low20 {low20:.2f}",
                },
            }

    return None


_STRATEGY_FINDERS = {
    "trend_pullback": _signal_trend_pullback,
    "mean_reversion": _signal_mean_reversion,
    "breakout": _signal_breakout,
}


def _scan_for_signals(exchange) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        try:
            data = _analyze_symbol(exchange, symbol)
            if not data:
                continue
            for strategy_name, finder in _STRATEGY_FINDERS.items():
                result = finder(data)
                if not result:
                    continue
                # Score gate: skip setups weaker than MIN_ENTRY_SCORE.
                if float(result.get("score") or 0) < MIN_ENTRY_SCORE:
                    continue
                signals.append({
                    "symbol": symbol,
                    "strategy": strategy_name,
                    "current": data["current"],
                    "atr": data["atr"],
                    "regime": data.get("regime", "range"),
                    "atr_avg": data.get("atr_avg"),
                    "atr_extreme": data.get("atr_extreme", False),
                    # Entry-TF context carried for the thinking layer (confluence/structure).
                    "high20": data.get("high20"),
                    "low20": data.get("low20"),
                    "ema9": data.get("ema9"),
                    "ema30": data.get("ema30"),
                    "ema50": data.get("ema50"),
                    **result,
                })
        except Exception:
            continue
    # Prefer the highest-conviction signal first.
    signals.sort(key=lambda s: float(s.get("score") or 0), reverse=True)
    return signals


# ─── Trade lifecycle ──────────────────────────────────────────────────────────

def _open_trade(
    lab: dict[str, Any],
    signal: dict[str, Any],
    risk_pct: float = RISK_PER_TRADE * 100,
    assigned_risk_pct: float | None = None,
    was_trimmed: bool = False,
    portfolio_risk_before: float = 0.0,
) -> dict[str, Any]:
    deposit = float(lab["virtual_usdt"])
    entry_price = float(signal["current"])
    stop_price = float(signal["stop_price"])
    side = signal["side"]
    symbol = signal["symbol"]
    strategy = signal["strategy"]
    atr = float(signal.get("atr") or 0)

    actual_risk_pct = float(risk_pct)
    if assigned_risk_pct is None:
        assigned_risk_pct = actual_risk_pct
    score = signal.get("score")
    score_components = signal.get("score_components") or {}

    position_size_usdt = _calc_position_size(deposit, entry_price, stop_price, actual_risk_pct)
    liq_price = _liquidation_price(entry_price, side, LEVERAGE)

    risk_distance = abs(entry_price - stop_price)
    tp_price = (
        entry_price + TP_RISK_RATIO * risk_distance
        if side == "long"
        else entry_price - TP_RISK_RATIO * risk_distance
    )

    regime = signal.get("regime", "range")
    btc_regime = signal.get("btc_regime")
    coin_regime = signal.get("coin_regime")
    consecutive_losses = int(lab.get("consecutive_losses") or 0)
    day_loss_pct = round(_current_day_loss_pct(lab), 3)
    entry_reason = dict(signal.get("entry_reason") or {})
    entry_reason["market_regime"] = regime
    if btc_regime is not None:
        entry_reason["btc_regime"] = btc_regime
    if coin_regime is not None:
        entry_reason["coin_regime"] = coin_regime

    risk_fields = {
        "score": score,
        "score_components": score_components,
        "assigned_risk_pct": round(float(assigned_risk_pct), 4),
        "actual_risk_pct": round(actual_risk_pct, 4),
        "was_trimmed": bool(was_trimmed),
        "portfolio_risk_before": round(float(portfolio_risk_before), 4),
        # Thinking-layer assessment (trends per TF, confluence, structure levels) so
        # each layer can be correlated against the trade outcome offline.
        "thinking": dict(signal.get("thinking") or {}),
    }

    trade: dict[str, Any] = {
        "strategy": strategy,
        "side": side,
        "symbol": symbol,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "trailing_stop": stop_price,
        "trailing_activated": False,
        "take_profit_price": tp_price,
        "liquidation_price": liq_price,
        "position_size_usdt": position_size_usdt,
        "leverage": LEVERAGE,
        "deposit_before": deposit,
        "entry_reason": entry_reason,
        "market_regime": regime,
        "btc_regime": btc_regime,
        "coin_regime": coin_regime,
        "consecutive_losses": consecutive_losses,
        "day_loss_pct": day_loss_pct,
        "atr": atr,
        "highest_price": entry_price,
        "lowest_price": entry_price,
        "opened_at": now_iso(),
        **risk_fields,
    }

    lab["active_trade"] = trade

    _append_journal(lab, {
        "action": "OPEN",
        "time": now_iso(),
        "strategy": strategy,
        "side": side,
        "symbol": symbol,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "take_profit_price": tp_price,
        "liquidation_price": liq_price,
        "position_size_usdt": position_size_usdt,
        "leverage": LEVERAGE,
        "deposit_before": deposit,
        "entry_reason": entry_reason,
        "market_regime": regime,
        "btc_regime": btc_regime,
        "coin_regime": coin_regime,
        "consecutive_losses": consecutive_losses,
        "day_loss_pct": day_loss_pct,
        "opened_at": trade["opened_at"],
        **risk_fields,
    })

    return lab


def _close_trade(
    lab: dict[str, Any],
    exit_price: float,
    reason: str,
    notifications: list[str] | None = None,
) -> dict[str, Any]:
    trade = lab.get("active_trade")
    if not trade:
        return lab

    deposit_before = float(trade["deposit_before"])
    entry_price = float(trade["entry_price"])
    position_size_usdt = float(trade["position_size_usdt"])
    side = trade["side"]

    if side == "long":
        pnl_usdt = position_size_usdt * (exit_price - entry_price) / entry_price
    else:
        pnl_usdt = position_size_usdt * (entry_price - exit_price) / entry_price

    pnl_pct = pnl_usdt / deposit_before * 100 if deposit_before else 0.0
    deposit_after = deposit_before + pnl_usdt
    lab["virtual_usdt"] = deposit_after

    duration_hours = 0.0
    try:
        opened_at = datetime.fromisoformat(trade["opened_at"])
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        duration_hours = round(
            (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600, 2
        )
    except Exception:
        pass

    closed_at = now_iso()

    _append_journal(lab, {
        "action": "CLOSE",
        "time": closed_at,
        "strategy": trade["strategy"],
        "side": side,
        "symbol": trade["symbol"],
        "entry_price": entry_price,
        "stop_price": trade["stop_price"],
        "take_profit_price": trade.get("take_profit_price"),
        "liquidation_price": trade.get("liquidation_price"),
        "position_size_usdt": position_size_usdt,
        "leverage": LEVERAGE,
        "entry_reason": trade.get("entry_reason", {}),
        "market_regime": trade.get("market_regime"),
        "btc_regime": trade.get("btc_regime"),
        "coin_regime": trade.get("coin_regime"),
        # Carry entry-time score/risk onto the outcome row for offline factor analysis.
        "score": trade.get("score"),
        "score_components": trade.get("score_components"),
        "assigned_risk_pct": trade.get("assigned_risk_pct"),
        "actual_risk_pct": trade.get("actual_risk_pct"),
        "was_trimmed": trade.get("was_trimmed"),
        "portfolio_risk_before": trade.get("portfolio_risk_before"),
        "thinking": trade.get("thinking"),
        "exit_price": exit_price,
        "exit_reason": reason,
        "pnl_pct": round(pnl_pct, 4),
        "pnl_usdt": round(pnl_usdt, 4),
        "duration_hours": duration_hours,
        "deposit_before": deposit_before,
        "deposit_after": round(deposit_after, 4),
        "opened_at": trade.get("opened_at"),
        "closed_at": closed_at,
    })

    # ── Circuit-breaker bookkeeping ──
    now = datetime.now(timezone.utc)

    # Consecutive-loss tracking: any profitable trade resets the counter.
    if pnl_usdt > 0:
        lab["consecutive_losses"] = 0
    else:
        streak = int(lab.get("consecutive_losses") or 0) + 1
        lab["consecutive_losses"] = streak
        if streak >= MAX_CONSECUTIVE_LOSSES:
            until = now + timedelta(hours=COOLDOWN_AFTER_LOSSES_HOURS)
            lab["losses_cooldown_until"] = until.isoformat()
            # Reset the counter so the pause grants a fresh streak afterwards.
            lab["consecutive_losses"] = 0
            if notifications is not None:
                notifications.append(
                    f"⏸ {MAX_CONSECUTIVE_LOSSES} убытка подряд. "
                    f"Пауза на {COOLDOWN_AFTER_LOSSES_HOURS}ч — рынок неблагоприятный"
                )

    # Per-symbol stop cooldown after any stop-out / liquidation / emergency exit.
    if reason in ("stop", "trailing_stop", "liquidation", "pump_guard"):
        cds = lab.setdefault("stop_cooldowns", {})
        cds[trade["symbol"]] = (now + timedelta(hours=STOP_COOLDOWN_HOURS)).isoformat()

    lab["active_trade"] = None
    return lab


# ─── Main tick ────────────────────────────────────────────────────────────────

def futures_tick() -> dict[str, Any]:
    exchange = make_futures_exchange()
    lab = load_futures_state()
    lab["last_tick"] = now_iso()
    lab["last_event"] = None
    lab = _roll_day(lab)
    now = datetime.now(timezone.utc)
    notifications: list[str] = []

    if not lab.get("enabled", True):
        save_futures_state(lab)
        return {
            "status": "disabled",
            "virtual_usdt": float(lab.get("virtual_usdt") or 0),
            "notifications": notifications,
        }

    active = lab.get("active_trade")

    if active:
        symbol = active["symbol"]
        try:
            ticker = exchange.fetch_ticker(symbol)
            current_price = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception as exc:
            save_futures_state(lab)
            return {"status": "error", "error": str(exc), "notifications": notifications}

        if current_price <= 0:
            save_futures_state(lab)
            return {"status": "holding", "note": "price=0", "notifications": notifications}

        side = active["side"]
        entry_price = float(active["entry_price"])
        atr = float(active.get("atr") or 0)
        position_size_usdt = float(active["position_size_usdt"])
        deposit_before = float(active["deposit_before"])
        stop_mult = _stop_mult(side)

        # ── Pump guard: emergency-exit a short on a sharp 1h pump against us ──
        if side == "short":
            try:
                pump_candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=3)
                pump_pct = _short_pump_pct(pump_candles, current_price)
                if pump_pct >= PUMP_GUARD_PCT:
                    lab = _close_trade(lab, current_price, "pump_guard", notifications)
                    notifications.append(
                        f"🚨 Экстренный выход из шорта — памп +{pump_pct:.1f}% за час"
                    )
                    save_futures_state(lab)
                    last = lab["journal"][-1] if lab["journal"] else {}
                    return {
                        "status": "closed",
                        "reason": "pump_guard",
                        "symbol": symbol,
                        "pump_pct": round(pump_pct, 2),
                        "pnl_usdt": last.get("pnl_usdt"),
                        "virtual_usdt": float(lab["virtual_usdt"]),
                        "event": lab.get("last_event"),
                        "notifications": notifications,
                    }
            except Exception:
                pass

        # Update trailing stop (short uses the tighter asymmetric distance)
        if atr > 0:
            if side == "long":
                highest = max(float(active.get("highest_price") or entry_price), current_price)
                active["highest_price"] = highest
                if current_price >= entry_price + stop_mult * atr:
                    active["trailing_activated"] = True
                if active.get("trailing_activated"):
                    new_trail = highest - stop_mult * atr
                    active["trailing_stop"] = max(
                        new_trail, float(active.get("trailing_stop") or active["stop_price"])
                    )
            else:
                lowest = min(float(active.get("lowest_price") or entry_price), current_price)
                active["lowest_price"] = lowest
                if current_price <= entry_price - stop_mult * atr:
                    active["trailing_activated"] = True
                if active.get("trailing_activated"):
                    new_trail = lowest + stop_mult * atr
                    active["trailing_stop"] = min(
                        new_trail, float(active.get("trailing_stop") or active["stop_price"])
                    )

        lab["active_trade"] = active

        effective_stop = float(active.get("trailing_stop") or active["stop_price"])
        tp_price = float(active.get("take_profit_price") or 0)
        liq_price = float(active.get("liquidation_price") or 0)

        # Check liquidation first (most critical)
        liq_hit = (side == "long" and liq_price > 0 and current_price <= liq_price) or (
            side == "short" and liq_price > 0 and current_price >= liq_price
        )
        if liq_hit:
            lab = _close_trade(lab, liq_price, "liquidation", notifications)
            save_futures_state(lab)
            last = lab["journal"][-1] if lab["journal"] else {}
            return {
                "status": "closed",
                "reason": "liquidation",
                "symbol": symbol,
                "pnl_usdt": last.get("pnl_usdt"),
                "virtual_usdt": float(lab["virtual_usdt"]),
                "event": lab.get("last_event"),
                "notifications": notifications,
            }

        # Check stop
        stop_hit = (side == "long" and current_price <= effective_stop) or (
            side == "short" and current_price >= effective_stop
        )
        if stop_hit:
            reason = "trailing_stop" if active.get("trailing_activated") else "stop"
            lab = _close_trade(lab, current_price, reason, notifications)
            save_futures_state(lab)
            last = lab["journal"][-1] if lab["journal"] else {}
            return {
                "status": "closed",
                "reason": reason,
                "symbol": symbol,
                "pnl_usdt": last.get("pnl_usdt"),
                "virtual_usdt": float(lab["virtual_usdt"]),
                "event": lab.get("last_event"),
                "notifications": notifications,
            }

        # Check take profit
        tp_hit = (side == "long" and tp_price > 0 and current_price >= tp_price) or (
            side == "short" and tp_price > 0 and current_price <= tp_price
        )
        if tp_hit:
            lab = _close_trade(lab, current_price, "take", notifications)
            save_futures_state(lab)
            last = lab["journal"][-1] if lab["journal"] else {}
            return {
                "status": "closed",
                "reason": "take",
                "symbol": symbol,
                "pnl_usdt": last.get("pnl_usdt"),
                "virtual_usdt": float(lab["virtual_usdt"]),
                "event": lab.get("last_event"),
                "notifications": notifications,
            }

        save_futures_state(lab)

        if side == "long":
            unrealized_pnl = position_size_usdt * (current_price - entry_price) / entry_price
        else:
            unrealized_pnl = position_size_usdt * (entry_price - current_price) / entry_price

        return {
            "status": "holding",
            "strategy": active["strategy"],
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "current_price": current_price,
            "stop": effective_stop,
            "tp": tp_price,
            "trailing_activated": active.get("trailing_activated", False),
            "unrealized_pnl_usdt": round(unrealized_pnl, 2),
            "unrealized_pct": round(unrealized_pnl / deposit_before * 100, 2) if deposit_before else 0,
            "virtual_usdt": float(lab["virtual_usdt"]),
            "notifications": notifications,
        }

    # No active trade — check account-wide circuit breakers before scanning
    gblock = _global_entry_block(lab, now)
    if gblock:
        _log_blocked_entry(lab, "*", "-", "-", "-", gblock)
        if gblock == "daily_loss_limit" and lab.get("daily_limit_notified_date") != _today_utc():
            lab["daily_limit_notified_date"] = _today_utc()
            notifications.append(
                f"⛔ Дневной лимит убытка достигнут (-{DAILY_LOSS_LIMIT_PCT:.0f}%). "
                f"Входы остановлены до завтра"
            )
        save_futures_state(lab)
        return {
            "status": "blocked",
            "reason": gblock,
            "day_loss_pct": round(_current_day_loss_pct(lab), 2),
            "virtual_usdt": float(lab.get("virtual_usdt") or 0),
            "notifications": notifications,
        }

    # Macro 4h direction filter (hourly cached): both BTC and the coin must agree
    # on a trend before any entry. FLAT on either → stand aside (sideways market).
    regimes = _get_macro_regimes(exchange, lab, now)
    btc_regime = regimes.get("btc", "FLAT")
    coin_regimes = regimes.get("coins") or {}

    # Scan for entry signals; pick the first that passes per-signal filters
    signals = _scan_for_signals(exchange)
    chosen = None
    blocked: list[dict[str, Any]] = []
    thinking_cache: dict[str, dict] = {}  # per-symbol MTF/structure data, one fetch per tick
    for sig in signals:
        coin_regime = coin_regimes.get(sig["symbol"], "FLAT")
        # NEW: strict trend-direction gate, applied on top of the existing breakers.
        dblock = _direction_filter_block(sig["side"], btc_regime, coin_regime)
        if dblock:
            _log_blocked_entry(
                lab, sig["symbol"], sig["side"], sig["strategy"],
                f"btc={btc_regime}/coin={coin_regime}", dblock,
            )
            blocked.append({"symbol": sig["symbol"], "side": sig["side"], "reason": dblock})
            continue
        sblock = _signal_entry_block(lab, sig, now)
        if sblock:
            _log_blocked_entry(
                lab, sig["symbol"], sig["side"], sig["strategy"], sig.get("regime", "range"), sblock
            )
            blocked.append({"symbol": sig["symbol"], "side": sig["side"], "reason": sblock})
            continue
        # Thinking layer: three quality filters ON TOP of direction+breakers.
        tblock, which, tdetails, tassess = _apply_thinking_filters(
            exchange, sig, coin_regime, thinking_cache
        )
        if tblock:
            _log_blocked_entry(
                lab, sig["symbol"], sig["side"], sig["strategy"],
                f"btc={btc_regime}/coin={coin_regime}", tblock,
                which_filter=which, details=tdetails,
            )
            blocked.append({
                "symbol": sig["symbol"], "side": sig["side"],
                "reason": tblock, "which_filter": which,
            })
            continue
        # Tag the chosen signal with the macro regimes + thinking assessment for journaling.
        sig["btc_regime"] = btc_regime
        sig["coin_regime"] = coin_regime
        sig["thinking"] = tassess
        chosen = sig
        break

    if chosen:
        portfolio_risk_before = _current_portfolio_risk_pct(lab)
        actual_risk, assigned_risk, was_trimmed, rblock = _resolve_entry_risk(
            float(chosen.get("score") or 0), portfolio_risk_before
        )
        if rblock:
            _log_blocked_entry(
                lab, chosen["symbol"], chosen["side"], chosen["strategy"],
                chosen.get("regime", "range"), rblock,
            )
            if rblock == "portfolio_risk_ceiling":
                notifications.append(
                    f"🚧 Портфельный потолок риска исчерпан "
                    f"({portfolio_risk_before:.2f}% из {MAX_PORTFOLIO_RISK_PCT:.0f}%) — вход заблокирован"
                )
            save_futures_state(lab)
            return {
                "status": "blocked",
                "reason": rblock,
                "symbol": chosen["symbol"],
                "side": chosen["side"],
                "portfolio_risk": round(portfolio_risk_before, 2),
                "virtual_usdt": float(lab.get("virtual_usdt") or 0),
                "notifications": notifications,
            }

        lab = _open_trade(
            lab, chosen, actual_risk, assigned_risk, was_trimmed, portfolio_risk_before
        )
        if was_trimmed:
            notifications.append(
                f"✂️ Размер урезан с {assigned_risk:.1f}% до {actual_risk:.2f}% "
                f"из-за потолка риска портфеля ({MAX_PORTFOLIO_RISK_PCT:.0f}%)"
            )
        save_futures_state(lab)
        return {
            "status": "opened",
            "strategy": chosen["strategy"],
            "symbol": chosen["symbol"],
            "side": chosen["side"],
            "regime": chosen.get("regime"),
            "btc_regime": chosen.get("btc_regime"),
            "coin_regime": chosen.get("coin_regime"),
            "score": chosen.get("score"),
            "assigned_risk_pct": assigned_risk,
            "actual_risk_pct": actual_risk,
            "was_trimmed": was_trimmed,
            "entry_price": chosen["current"],
            "stop_price": chosen["stop_price"],
            "virtual_usdt": float(lab["virtual_usdt"]),
            "event": lab.get("last_event"),
            "notifications": notifications,
        }

    save_futures_state(lab)
    return {
        "status": "idle",
        "virtual_usdt": float(lab.get("virtual_usdt") or 0),
        "signals_checked": len(SYMBOLS) * len(_STRATEGY_FINDERS),
        "btc_regime": btc_regime,
        "coin_regimes": coin_regimes,
        "blocked_entries": blocked,
        "notifications": notifications,
    }


# ─── Public reporting ─────────────────────────────────────────────────────────

def _per_strategy_stats(closed: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for row in closed:
        s = row.get("strategy") or "unknown"
        if s not in stats:
            stats[s] = {"count": 0, "wins": 0, "losses": 0, "pnl_usdt": 0.0}
        stats[s]["count"] += 1
        pnl = float(row.get("pnl_usdt") or 0)
        stats[s]["pnl_usdt"] += pnl
        if pnl > 0:
            stats[s]["wins"] += 1
        else:
            stats[s]["losses"] += 1
    for s in stats:
        cnt = stats[s]["count"]
        stats[s]["winrate"] = stats[s]["wins"] / cnt * 100 if cnt else 0.0
    return stats


def futures_summary() -> str:
    lab = load_futures_state()
    initial = float(lab.get("initial_usdt") or VIRTUAL_USDT_START)
    current = float(lab.get("virtual_usdt") or VIRTUAL_USDT_START)
    growth_pct = (current / initial - 1) * 100 if initial else 0.0

    journal = lab.get("journal") or []
    closed = [x for x in journal if x.get("action") == "CLOSE"]
    wins = [x for x in closed if float(x.get("pnl_usdt") or 0) > 0]
    winrate = len(wins) / len(closed) * 100 if closed else 0.0

    per_strat = _per_strategy_stats(closed)

    lines = [
        "📈 FUTURES LAB (виртуальный)",
        "",
        f"Включён: {'да' if lab.get('enabled', True) else 'нет'}",
        f"Таймфрейм: {TIMEFRAME} | Плечо: {LEVERAGE}x | "
        f"Риск/сделка: 1-3% по score | Потолок портфеля: {MAX_PORTFOLIO_RISK_PCT:.0f}%",
        f"Пары: {', '.join(SYMBOLS)}",
        f"Депо старт: ${initial:.2f}",
        f"Депо сейчас: ${current:.2f}",
        f"Результат: {growth_pct:+.2f}%",
        "",
        f"Закрытых сделок: {len(closed)}",
        f"Плюсовых: {len(wins)} | Минусовых: {len(closed) - len(wins)}",
        f"Общий винрейт: {winrate:.1f}%",
        f"Последний тик: {lab.get('last_tick') or 'нет'}",
    ]

    if per_strat:
        lines += ["", "Винрейт по стратегиям:"]
        for name in ("trend_pullback", "mean_reversion", "breakout"):
            st = per_strat.get(name)
            if st:
                lines.append(
                    f"  {name}: {st['count']} сделок | "
                    f"WR {st['winrate']:.1f}% | "
                    f"PnL ${st['pnl_usdt']:+.2f}"
                )

    active = lab.get("active_trade")
    if active:
        lines += [
            "",
            "Открытая позиция:",
            f"  Стратегия: {active['strategy']}",
            f"  Пара: {active['symbol']} | Сторона: {active['side'].upper()}",
            f"  Вход: {active['entry_price']} | Стоп: {float(active.get('trailing_stop') or active['stop_price']):.2f}",
            f"  TP: {active.get('take_profit_price', '?')} | Лик: {active.get('liquidation_price', '?'):.2f}",
            f"  Позиция: ${float(active['position_size_usdt']):.2f} (нотионал) | Плечо: {LEVERAGE}x",
            f"  Трейлинг: {'активен' if active.get('trailing_activated') else 'ждёт'}",
            f"  Открыта: {active.get('opened_at')}",
        ]
    else:
        lines += ["", "Открытая позиция: нет"]

    return "\n".join(lines)


def circuit_status() -> str:
    """Current state of all circuit breakers."""
    lab = load_futures_state()
    lab = _roll_day(lab)
    save_futures_state(lab)

    now = datetime.now(timezone.utc)
    day_loss = _current_day_loss_pct(lab)
    cons = int(lab.get("consecutive_losses") or 0)

    lines = [
        "🛡 CIRCUIT BREAKERS",
        "",
        f"Дневной убыток: -{day_loss:.2f}% из {DAILY_LOSS_LIMIT_PCT:.0f}%"
        + ("  ⛔ ЛИМИТ" if day_loss >= DAILY_LOSS_LIMIT_PCT else ""),
        f"Депо на начало дня (UTC {lab.get('day_start_date')}): "
        f"${float(lab.get('day_start_balance') or 0):.2f}",
        f"Убытков подряд: {cons} из {MAX_CONSECUTIVE_LOSSES}",
    ]

    # Cooldowns
    losses_until = _parse_iso(lab.get("losses_cooldown_until"))
    if losses_until and now < losses_until:
        mins = (losses_until - now).total_seconds() / 60
        lines.append(f"⏸ Пауза после серии убытков: ещё {mins/60:.1f}ч (до {losses_until.isoformat()})")
    else:
        lines.append("⏸ Пауза после серии убытков: нет")

    stop_cds = lab.get("stop_cooldowns") or {}
    active_cds = []
    for sym, until_s in stop_cds.items():
        until = _parse_iso(until_s)
        if until and now < until:
            mins = (until - now).total_seconds() / 60
            active_cds.append(f"  {sym}: ещё {mins:.0f}мин")
    if active_cds:
        lines.append("🚫 Cooldown после стопа:")
        lines += active_cds
    else:
        lines.append("🚫 Cooldown после стопа: нет")

    # Portfolio risk ceiling
    deposit = float(lab.get("virtual_usdt") or 0)
    port_risk = _current_portfolio_risk_pct(lab)
    remaining = MAX_PORTFOLIO_RISK_PCT - port_risk
    lines += [
        "",
        f"Риск портфеля: {port_risk:.2f}% из {MAX_PORTFOLIO_RISK_PCT:.0f}% "
        f"(остаток {remaining:.2f}%)",
    ]
    positions = _open_positions(lab)
    if positions:
        lines.append("Открытые позиции:")
        for t in positions:
            r = _position_risk_pct(t, deposit)
            stop = float(t.get("trailing_stop") or t.get("stop_price") or 0)
            sc = t.get("score")
            lines.append(
                f"  {t.get('symbol')} {t.get('side')} [{t.get('strategy')}] "
                f"риск {r:.2f}%"
                + (f" | score {sc}" if sc is not None else "")
                + f" | вход {t.get('entry_price')} стоп {stop:.2f}"
                + ("  ✂️" if t.get("was_trimmed") else "")
            )
    else:
        lines.append("Открытые позиции: нет")

    # Macro direction filter (4h EMA50/EMA200) — the strict trend gate on new entries
    exchange = None
    try:
        exchange = make_futures_exchange()
    except Exception as exc:
        lines += ["", f"Фильтр тренда (4ч): ошибка биржи: {exc}"]

    if exchange is not None:
        try:
            btc_info = market_regime_analysis(exchange, BTC_SYMBOL)
            btc_regime = btc_info.get("regime", "FLAT")
            lines += [
                "",
                "🧭 Фильтр направления (4ч EMA50/EMA200):",
                f"  BTC (барометр): {btc_regime}",
            ]
            for symbol in SYMBOLS:
                try:
                    info = (
                        btc_info if symbol == BTC_SYMBOL
                        else market_regime_analysis(exchange, symbol)
                    )
                    coin_regime = info.get("regime", "FLAT")
                    allowed = _allowed_now(btc_regime, coin_regime)
                    lines.append(f"  {symbol}: {coin_regime} → разрешено: {allowed}")
                except Exception as exc:
                    lines.append(f"  {symbol}: ошибка {exc}")
            if btc_regime == "FLAT":
                lines.append("  ⚠️ BTC в боковике — новые входы остановлены по всем монетам")
        except Exception as exc:
            lines.append(f"  ошибка фильтра тренда: {exc}")

        # Live market regimes for the watched symbols (1h EMA50 — legacy soft filter)
        lines += ["", "Режим рынка (EMA50 1ч):"]
        for symbol in SYMBOLS:
            try:
                data = _analyze_symbol(exchange, symbol)
                if not data:
                    lines.append(f"  {symbol}: нет данных")
                    continue
                regime = data.get("regime", "range")
                long_ok = _entry_allowed_by_regime("long", regime)
                short_ok = _entry_allowed_by_regime("short", regime)
                vol_block = " | шорт заблокирован (волатильность)" if data.get("atr_extreme") else ""
                lines.append(
                    f"  {symbol}: {regime} "
                    f"(лонг {'✅' if long_ok else '⛔'} / шорт {'✅' if short_ok and not data.get('atr_extreme') else '⛔'})"
                    f"{vol_block}"
                )
            except Exception as exc:
                lines.append(f"  {symbol}: ошибка {exc}")

    # Recent blocked entries
    blocked = lab.get("blocked_entries") or []
    if blocked:
        lines += ["", f"Последние блокировки входа ({len(blocked)} всего):"]
        for b in blocked[-5:]:
            lines.append(
                f"  {b.get('time', '')[:19]} {b.get('symbol')} "
                f"{b.get('side')} [{b.get('regime')}] → {b.get('reason')}"
            )

    return "\n".join(lines)


def _blocked_within_days(blocked: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for b in blocked:
        t = _parse_iso(b.get("time"))
        if t is None or t >= cutoff:
            out.append(b)
    return out


def thinking_status(days: int = 7) -> str:
    """Per-filter attribution: how many entries each thinking layer blocked and why."""
    lab = load_futures_state()
    blocked = _blocked_within_days(lab.get("blocked_entries") or [], days)

    layers = (
        ("mtf", "Слой 1 — MTF (мультитаймфрейм)", MTF_FILTER_ON),
        ("confluence", "Слой 2 — Конфлюэнция", CONFLUENCE_FILTER_ON),
        ("structure", "Слой 3 — Структура (S/R)", STRUCTURE_FILTER_ON),
    )

    thinking_blocks = [b for b in blocked if b.get("which_filter") in {"mtf", "confluence", "structure"}]

    lines = [
        "🧠 THINKING — фильтры качества входа",
        f"(период: {days}д | всего блокировок слоями: {len(thinking_blocks)})",
        "",
    ]

    for key, label, flag_on in layers:
        rows = [b for b in thinking_blocks if b.get("which_filter") == key]
        reasons: dict[str, int] = {}
        for b in rows:
            reasons[b.get("reason", "?")] = reasons.get(b.get("reason", "?"), 0) + 1
        state = "🟢 ON" if flag_on else "⚪️ OFF"
        lines.append(f"{label}: {state} — заблокировал {len(rows)}")
        for reason, cnt in sorted(reasons.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"    • {reason}: {cnt}")

    # A couple of concrete recent examples for context.
    if thinking_blocks:
        lines += ["", "Последние блокировки:"]
        for b in thinking_blocks[-5:]:
            det = b.get("details") or {}
            extra = ""
            if b.get("which_filter") == "mtf":
                extra = f" 1d={det.get('trend_1d')}/4h={det.get('trend_4h')}/1h={det.get('trend_1h')}"
            elif b.get("which_filter") == "confluence":
                extra = f" score={det.get('confluence_score')} {det.get('factors')}"
            elif b.get("which_filter") == "structure":
                extra = f" dist={det.get('distance_to_level_atr')}ATR"
            lines.append(
                f"  {str(b.get('time', ''))[:19]} {b.get('symbol')} {b.get('side')} "
                f"→ [{b.get('which_filter')}] {b.get('reason')}{extra}"
            )
    else:
        lines += ["", "Слои пока ничего не отсекли за период."]

    return "\n".join(lines)


def _filter_closed_by_days(journal: list, days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for row in journal:
        if row.get("action") != "CLOSE":
            continue
        try:
            t_str = row.get("closed_at") or row.get("time") or ""
            t = datetime.fromisoformat(t_str)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t >= cutoff:
                result.append(row)
        except Exception:
            continue
    return result


def futures_report(days: int = 7) -> str:
    lab = load_futures_state()
    journal = lab.get("journal") or []
    closed = _filter_closed_by_days(journal, days)

    if not closed:
        return f"📊 FUTURES REPORT ({days}д)\n\nСделок за период нет."

    per_strat = _per_strategy_stats(closed)
    total_pnl = sum(float(r.get("pnl_usdt") or 0) for r in closed)
    total_wins = sum(1 for r in closed if float(r.get("pnl_usdt") or 0) > 0)
    total_wr = total_wins / len(closed) * 100

    lines = [
        f"📊 FUTURES REPORT ({days}д)",
        f"Всего сделок: {len(closed)} | Винрейт: {total_wr:.1f}% | PnL: ${total_pnl:+.2f}",
        "",
    ]

    for name in ("trend_pullback", "mean_reversion", "breakout"):
        st = per_strat.get(name)
        if not st:
            lines.append(f"▪ {name}: нет сделок")
            continue
        lines.append(
            f"▪ {name}\n"
            f"  Сделок: {st['count']} | WR: {st['winrate']:.1f}%\n"
            f"  PnL: ${st['pnl_usdt']:+.2f} | "
            f"W:{st['wins']} / L:{st['losses']}"
        )

    # Top/worst trades
    sorted_trades = sorted(closed, key=lambda x: float(x.get("pnl_usdt") or 0), reverse=True)
    top3 = sorted_trades[:3]
    worst3 = list(reversed(sorted_trades[-3:]))

    lines += ["", "Топ-3 лучших:"]
    for i, t in enumerate(top3, 1):
        lines.append(
            f"  {i}. {t.get('symbol')} {t.get('side')} [{t.get('strategy')}] "
            f"${float(t.get('pnl_usdt') or 0):+.2f} | {t.get('exit_reason')}"
        )

    lines += ["", "Топ-3 худших:"]
    for i, t in enumerate(worst3, 1):
        lines.append(
            f"  {i}. {t.get('symbol')} {t.get('side')} [{t.get('strategy')}] "
            f"${float(t.get('pnl_usdt') or 0):+.2f} | {t.get('exit_reason')}"
        )

    return "\n".join(lines)


def export_futures_csv(days: int = 30) -> Path:
    lab = load_futures_state()
    journal = lab.get("journal") or []
    closed = _filter_closed_by_days(journal, days)

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"futures_{days}d_{ts}.csv"

    fieldnames = [
        "strategy", "side", "symbol",
        "market_regime", "btc_regime", "coin_regime", "consecutive_losses", "day_loss_pct",
        "score", "sc_trend", "sc_magnitude", "sc_regime", "sc_volatility",
        "assigned_risk_pct", "actual_risk_pct", "was_trimmed", "portfolio_risk_before",
        "entry_price", "stop_price", "take_profit_price", "liquidation_price",
        "position_size_usdt", "leverage",
        "exit_price", "exit_reason",
        "pnl_pct", "pnl_usdt",
        "duration_hours", "deposit_before", "deposit_after",
        "opened_at", "closed_at",
        "er_condition", "er_ema9", "er_ema30", "er_atr", "er_deviation",
        "er_high20", "er_low20", "er_breakout_pct",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in closed:
            er = row.get("entry_reason") or {}
            sc = row.get("score_components") or {}
            writer.writerow({
                "strategy": row.get("strategy"),
                "side": row.get("side"),
                "symbol": row.get("symbol"),
                "market_regime": row.get("market_regime"),
                "btc_regime": row.get("btc_regime"),
                "coin_regime": row.get("coin_regime"),
                "consecutive_losses": row.get("consecutive_losses"),
                "day_loss_pct": row.get("day_loss_pct"),
                "score": row.get("score"),
                "sc_trend": sc.get("trend"),
                "sc_magnitude": sc.get("magnitude"),
                "sc_regime": sc.get("regime"),
                "sc_volatility": sc.get("volatility"),
                "assigned_risk_pct": row.get("assigned_risk_pct"),
                "actual_risk_pct": row.get("actual_risk_pct"),
                "was_trimmed": row.get("was_trimmed"),
                "portfolio_risk_before": row.get("portfolio_risk_before"),
                "entry_price": row.get("entry_price"),
                "stop_price": row.get("stop_price"),
                "take_profit_price": row.get("take_profit_price"),
                "liquidation_price": row.get("liquidation_price"),
                "position_size_usdt": row.get("position_size_usdt"),
                "leverage": row.get("leverage"),
                "exit_price": row.get("exit_price"),
                "exit_reason": row.get("exit_reason"),
                "pnl_pct": row.get("pnl_pct"),
                "pnl_usdt": row.get("pnl_usdt"),
                "duration_hours": row.get("duration_hours"),
                "deposit_before": row.get("deposit_before"),
                "deposit_after": row.get("deposit_after"),
                "opened_at": row.get("opened_at"),
                "closed_at": row.get("closed_at"),
                "er_condition": er.get("condition"),
                "er_ema9": er.get("ema9"),
                "er_ema30": er.get("ema30"),
                "er_atr": er.get("atr"),
                "er_deviation": er.get("deviation"),
                "er_high20": er.get("high20"),
                "er_low20": er.get("low20"),
                "er_breakout_pct": er.get("breakout_pct"),
            })

    return path


def futures_reset() -> str:
    fresh = default_futures_state()
    update_portfolio(lambda s: s.update({FUTURES_KEY: fresh}))
    return f"📈 Futures Lab сброшен. Виртуальный баланс: ${VIRTUAL_USDT_START:.2f} USDT"


def futures_set_enabled(enabled: bool) -> str:
    lab = load_futures_state()
    lab["enabled"] = bool(enabled)
    save_futures_state(lab)
    return "📈 Futures Lab включён" if enabled else "📈 Futures Lab выключен"


def format_futures_event(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None

    action = event.get("action")
    symbol = event.get("symbol", "?")

    if action == "OPEN":
        er = event.get("entry_reason") or {}
        sc = event.get("score_components") or {}
        score = event.get("score")
        actual_risk = event.get("actual_risk_pct")
        assigned_risk = event.get("assigned_risk_pct")
        risk_line = ""
        if actual_risk is not None:
            risk_line = f"Риск: {float(actual_risk):.2f}%"
            if event.get("was_trimmed"):
                risk_line += f" (урезан с {float(assigned_risk or 0):.1f}%)"
            risk_line += "\n"
        score_line = ""
        if score is not None:
            score_line = (
                f"Score: {score} "
                f"(тренд {sc.get('trend', '?')}, объём {sc.get('magnitude', '?')}, "
                f"режим {sc.get('regime', '?')}, волат {sc.get('volatility', '?')})\n"
            )
        regime_line = ""
        if event.get("btc_regime") is not None or event.get("coin_regime") is not None:
            regime_line = (
                f"Тренд 4ч: BTC {event.get('btc_regime', '?')} / "
                f"монета {event.get('coin_regime', '?')}\n"
            )
        return (
            "📈 FUTURES DEMO ENTRY\n\n"
            f"Пара: {symbol} | {event.get('side', '').upper()}\n"
            f"Стратегия: {event.get('strategy')}\n"
            f"{regime_line}"
            f"{score_line}"
            f"Вход: {event.get('entry_price')}\n"
            f"Стоп: {event.get('stop_price')}\n"
            f"TP: {event.get('take_profit_price')}\n"
            f"Лик: {float(event.get('liquidation_price') or 0):.2f}\n"
            f"{risk_line}"
            f"Позиция: ${float(event.get('position_size_usdt') or 0):.2f} | x{LEVERAGE}\n"
            f"Условие: {er.get('condition', '')}\n"
            f"Депо: ${float(event.get('deposit_before') or 0):.2f}"
        )

    if action == "CLOSE":
        pnl = float(event.get("pnl_usdt") or 0)
        pnl_pct = float(event.get("pnl_pct") or 0)
        dep_before = float(event.get("deposit_before") or 0)
        dep_after = float(event.get("deposit_after") or 0)
        return (
            "📈 FUTURES DEMO EXIT\n\n"
            f"Пара: {symbol} | {event.get('side', '').upper()}\n"
            f"Стратегия: {event.get('strategy')}\n"
            f"Причина: {event.get('exit_reason')}\n"
            f"Вход: {event.get('entry_price')} → Выход: {event.get('exit_price')}\n"
            f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}% депо)\n"
            f"Длительность: {event.get('duration_hours')}ч\n"
            f"Депо: ${dep_before:.2f} → ${dep_after:.2f}"
        )

    return None
