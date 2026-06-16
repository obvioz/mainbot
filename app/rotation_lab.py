from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.market import make_exchange
from app.storage import load_portfolio, save_portfolio


ROTATION_KEY = "rotation_lab"

PAIRS_FILE = Path("data/bybit_pairs.json")
MAX_PAIRS = 10

DEFAULT_PAIRS = [
    "ETH/BTC",
    "SOL/BTC",
]

ALLOWED_ROTATION = [
    "ETH/BTC",
    "SOL/BTC",
    "LTC/BTC",
    "XRP/BTC",
]

TIMEFRAME = "1h"
CANDLE_LIMIT = 60

VIRTUAL_BTC_START = 1.0
MIN_ENTRY_SCORE = 60
MAX_JOURNAL_ROWS = 1000

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.0
PULLBACK_MIN_PCT = -0.3   # минимальный откат от 6-свечного максимума
PEAK_PENALTY_PCT = 0.5    # если цена ближе к пику чем 0.5% — штраф


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # trs[0] соответствует candles[1], поэтому первый валидный ATR — в atrs[period]
    atrs[period] = sum(trs[:period]) / period
    for i in range(period + 1, len(candles)):
        atrs[i] = (atrs[i - 1] * (period - 1) + trs[i - 1]) / period

    return atrs


def load_rotation_pairs(base: str = "BTC") -> list[str]:
    if not PAIRS_FILE.exists():
        return DEFAULT_PAIRS

    try:
        with open(PAIRS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        pairs = data.get(base, [])
        filtered = [pair for pair in pairs if pair in ALLOWED_ROTATION]
        return filtered[:MAX_PAIRS] or DEFAULT_PAIRS

    except Exception:
        return DEFAULT_PAIRS


def default_rotation_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": "simulation",
        "base": "BTC",
        "virtual_btc": VIRTUAL_BTC_START,
        "initial_btc": VIRTUAL_BTC_START,
        "active_trade": None,
        "journal": [],
        "last_tick": None,
        "last_event": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def load_rotation_state() -> dict[str, Any]:
    state = load_portfolio()
    lab = state.get(ROTATION_KEY)

    if not lab:
        lab = default_rotation_state()
        state[ROTATION_KEY] = lab
        save_portfolio(state)
        return lab

    lab.setdefault("enabled", True)
    lab.setdefault("mode", "simulation")
    lab.setdefault("base", "BTC")
    lab.setdefault("virtual_btc", VIRTUAL_BTC_START)
    lab.setdefault("initial_btc", VIRTUAL_BTC_START)
    lab.setdefault("active_trade", None)
    lab.setdefault("journal", [])
    lab.setdefault("last_tick", None)
    lab.setdefault("last_event", None)
    lab.setdefault("created_at", now_iso())
    lab.setdefault("updated_at", now_iso())

    return lab


def save_rotation_state(lab: dict[str, Any]) -> None:
    state = load_portfolio()
    lab["updated_at"] = now_iso()
    state[ROTATION_KEY] = lab
    save_portfolio(state)


def fetch_pair_price(exchange, pair: str) -> float:
    ticker = exchange.fetch_ticker(pair)
    return float(ticker.get("last") or ticker.get("close") or 0)


def analyze_pair(exchange, pair: str) -> dict[str, Any] | None:
    """Mean-reversion вход в тренде на 1h.

    Апутренд: EMA9 > EMA30 и цена выше EMA30.
    Вход: откат от 6-свечного максимума хотя бы на -0.3%.
    Штраф: если цена ближе 0.5% к пику — -20 к score.
    """
    candles = exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)

    if not candles or len(candles) < ATR_PERIOD + 5:
        return None

    closes = [float(c[4]) for c in candles]
    current = closes[-1]

    ema9_list = _compute_ema(closes, 9)
    ema30_list = _compute_ema(closes, 30)

    ema9 = ema9_list[-1]
    ema30 = ema30_list[-1]

    if math.isnan(ema9) or math.isnan(ema30) or ema30 <= 0:
        return None

    atrs = _compute_atr(candles, ATR_PERIOD)
    atr = atrs[-1]
    if math.isnan(atr) or atr <= 0:
        return None

    uptrend = ema9 > ema30 and current > ema30

    local_high = max(float(c[2]) for c in candles[-6:])
    pullback_pct = (current / local_high - 1) * 100 if local_high > 0 else 0.0

    volumes = [float(c[5]) for c in candles]
    avg_vol = sum(volumes[-13:-3]) / max(len(volumes[-13:-3]), 1)
    recent_vol = sum(volumes[-3:]) / max(len(volumes[-3:]), 1)
    volume_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    score = 0
    reasons: list[str] = []

    if uptrend:
        score += 40
        reasons.append("uptrend EMA9>EMA30")

    if pullback_pct <= PULLBACK_MIN_PCT:
        score += 30
        reasons.append(f"pullback {pullback_pct:+.2f}%")

    # штраф за нахождение у пика
    if pullback_pct > -PEAK_PENALTY_PCT:
        score -= 20
        reasons.append("near peak penalty")

    if not math.isnan(ema9_list[-1]) and not math.isnan(ema9_list[-3]) and ema9_list[-3] > 0:
        ema9_slope = (ema9_list[-1] / ema9_list[-3] - 1) * 100
        if ema9_slope > 0:
            score += 10
            reasons.append("EMA9 rising")

    if volume_ratio >= 1.2:
        score += 10
        reasons.append("volume up")

    return {
        "pair": pair,
        "price": current,
        "atr": atr,
        "ema9": ema9,
        "ema30": ema30,
        "uptrend": uptrend,
        "pullback_pct": pullback_pct,
        "local_high": local_high,
        "volume_ratio": volume_ratio,
        "score": score,
        "reasons": reasons,
    }


def analyze_rotation_market() -> list[dict[str, Any]]:
    exchange = make_exchange()
    rows: list[dict[str, Any]] = []

    for pair in load_rotation_pairs("BTC"):
        try:
            data = analyze_pair(exchange, pair)
            if data:
                rows.append(data)
        except Exception as exc:
            rows.append({"pair": pair, "error": str(exc), "score": 0})

    rows.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    return rows


def find_best_pair(exchange) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []

    for pair in load_rotation_pairs("BTC"):
        try:
            data = analyze_pair(exchange, pair)
            if data:
                candidates.append(data)
        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    best = candidates[0]

    if (
        float(best.get("score") or 0) >= MIN_ENTRY_SCORE
        and best.get("uptrend", False)
        and float(best.get("pullback_pct") or 0) <= PULLBACK_MIN_PCT
    ):
        return best

    return None


def _append_journal(lab: dict[str, Any], row: dict[str, Any]) -> None:
    lab.setdefault("journal", [])
    lab["journal"].append(row)
    lab["journal"] = lab["journal"][-MAX_JOURNAL_ROWS:]
    lab["last_event"] = row


def open_virtual_trade(lab: dict[str, Any], pair_data: dict[str, Any]) -> dict[str, Any]:
    btc_before = float(lab["virtual_btc"])
    atr = float(pair_data.get("atr") or 0)
    entry_price = float(pair_data["price"])
    initial_stop = entry_price - ATR_MULTIPLIER * atr

    trade: dict[str, Any] = {
        "pair": pair_data["pair"],
        "entry_price": entry_price,
        "atr": atr,
        "initial_stop": initial_stop,
        "trailing_stop": initial_stop,
        "trailing_activated": False,
        "highest_price": entry_price,
        "entry_score": int(pair_data.get("score") or 0),
        "entry_pullback_pct": float(pair_data.get("pullback_pct") or 0),
        "entry_uptrend": bool(pair_data.get("uptrend", False)),
        "entry_volume_ratio": float(pair_data.get("volume_ratio") or 0),
        "entry_reasons": pair_data.get("reasons") or [],
        "btc_amount": btc_before,
        "opened_at": now_iso(),
    }

    lab["active_trade"] = trade

    _append_journal(lab, {
        "time": now_iso(),
        "action": "OPEN",
        "pair": trade["pair"],
        "price": entry_price,
        "atr": atr,
        "initial_stop": initial_stop,
        "btc_before": btc_before,
        "btc_after": btc_before,
        "score": trade["entry_score"],
        "pullback_pct": trade["entry_pullback_pct"],
        "note": (
            f"score={trade['entry_score']} | "
            f"pullback={trade['entry_pullback_pct']:+.2f}% | "
            f"ATR={atr:.6f} | "
            f"stop={initial_stop:.6f}"
        ),
    })

    return lab


def close_virtual_trade(lab: dict[str, Any], exit_price: float, reason: str) -> dict[str, Any]:
    trade = lab.get("active_trade")
    if not trade:
        return lab

    btc_before = float(lab["virtual_btc"])
    entry_price = float(trade["entry_price"])

    if entry_price <= 0 or exit_price <= 0:
        return lab

    result_pct = (float(exit_price) / entry_price - 1) * 100
    btc_after = btc_before * (1 + result_pct / 100)
    lab["virtual_btc"] = btc_after

    _append_journal(lab, {
        "time": now_iso(),
        "action": "CLOSE",
        "pair": trade["pair"],
        "entry_price": entry_price,
        "exit_price": float(exit_price),
        "result_pct": result_pct,
        "btc_before": btc_before,
        "btc_after": btc_after,
        "reason": reason,
    })

    lab["active_trade"] = None
    return lab


def rotation_tick() -> dict[str, Any]:
    exchange = make_exchange()
    lab = load_rotation_state()
    lab["last_tick"] = now_iso()
    lab["last_event"] = None

    if not lab.get("enabled", True):
        save_rotation_state(lab)
        return {
            "status": "disabled",
            "virtual_btc": float(lab.get("virtual_btc") or 0),
        }

    active = lab.get("active_trade")

    if active:
        pair = active["pair"]
        current_price = fetch_pair_price(exchange, pair)
        entry_price = float(active["entry_price"])
        atr = float(active.get("atr") or 0)
        result_pct = (current_price / entry_price - 1) * 100 if entry_price else 0.0

        if atr > 0:
            highest = max(float(active.get("highest_price") or entry_price), current_price)
            active["highest_price"] = highest

            trail_activation = entry_price + ATR_MULTIPLIER * atr
            if current_price >= trail_activation:
                active["trailing_activated"] = True

            if active.get("trailing_activated"):
                new_trail = highest - ATR_MULTIPLIER * atr
                current_trail = float(active.get("trailing_stop") or active.get("initial_stop") or 0)
                active["trailing_stop"] = max(new_trail, current_trail)

            lab["active_trade"] = active

        stop = float(active.get("trailing_stop") or active.get("initial_stop") or 0)

        if stop > 0 and current_price <= stop:
            reason = "trailing_stop" if active.get("trailing_activated") else "stop_loss"
            lab = close_virtual_trade(lab, current_price, reason)
            save_rotation_state(lab)
            return {
                "status": "closed",
                "reason": reason,
                "pair": pair,
                "result_pct": result_pct,
                "virtual_btc": float(lab["virtual_btc"]),
                "event": lab.get("last_event"),
            }

        save_rotation_state(lab)
        return {
            "status": "holding",
            "pair": pair,
            "result_pct": result_pct,
            "trailing_activated": active.get("trailing_activated", False),
            "trailing_stop": active.get("trailing_stop"),
            "virtual_btc": float(lab["virtual_btc"]),
        }

    best = find_best_pair(exchange)

    if best:
        lab = open_virtual_trade(lab, best)
        save_rotation_state(lab)
        return {
            "status": "opened",
            "pair": best["pair"],
            "score": best.get("score"),
            "pullback_pct": best.get("pullback_pct"),
            "atr": best.get("atr"),
            "uptrend": best.get("uptrend"),
            "virtual_btc": float(lab["virtual_btc"]),
            "event": lab.get("last_event"),
        }

    save_rotation_state(lab)
    return {
        "status": "idle",
        "virtual_btc": float(lab.get("virtual_btc") or 0),
    }


def rotation_set_enabled(enabled: bool) -> str:
    lab = load_rotation_state()
    lab["enabled"] = bool(enabled)
    save_rotation_state(lab)
    return "🧪 Rotation Lab включён" if enabled else "🧪 Rotation Lab выключен"


def rotation_reset() -> str:
    state = load_portfolio()
    state[ROTATION_KEY] = default_rotation_state()
    save_portfolio(state)
    return "🧪 Rotation Lab сброшен. Виртуальный баланс снова 1.00000000 BTC"


def format_rotation_event(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None

    action = event.get("action")
    pair = event.get("pair")

    if action == "OPEN":
        return (
            "🧪 DEMO ENTRY\n\n"
            f"Пара: {pair}\n"
            f"Вход: {event.get('price')}\n"
            f"ATR: {float(event.get('atr') or 0):.6f}\n"
            f"Стоп: {float(event.get('initial_stop') or 0):.6f}\n"
            f"Score: {event.get('score')}\n"
            f"Откат: {float(event.get('pullback_pct') or 0):+.2f}%\n"
            f"BTC: {float(event.get('btc_before') or 0):.8f}"
        )

    if action == "CLOSE":
        return (
            "🧪 DEMO EXIT\n\n"
            f"Пара: {pair}\n"
            f"Причина: {event.get('reason')}\n"
            f"Результат: {float(event.get('result_pct') or 0):+.2f}%\n"
            f"BTC: {float(event.get('btc_before') or 0):.8f} → {float(event.get('btc_after') or 0):.8f}"
        )

    return None


def rotation_summary() -> str:
    lab = load_rotation_state()

    initial = float(lab.get("initial_btc") or 0)
    current = float(lab.get("virtual_btc") or 0)
    growth_pct = (current / initial - 1) * 100 if initial else 0.0

    journal = lab.get("journal") or []
    closed = [x for x in journal if x.get("action") == "CLOSE"]
    wins = [x for x in closed if float(x.get("result_pct") or 0) > 0]
    losses = [x for x in closed if float(x.get("result_pct") or 0) <= 0]
    winrate = len(wins) / len(closed) * 100 if closed else 0.0
    active = lab.get("active_trade")

    lines = [
        "🧪 ROTATION LAB",
        "",
        f"Режим: {lab.get('mode')}",
        f"Включён: {'да' if lab.get('enabled', True) else 'нет'}",
        f"База: {lab.get('base')}",
        f"Таймфрейм: {TIMEFRAME} | Логика: mean-reversion",
        f"Пары: {', '.join(load_rotation_pairs('BTC'))}",
        f"BTC старт: {initial:.8f}",
        f"BTC сейчас: {current:.8f}",
        f"Результат: {growth_pct:+.2f}%",
        "",
        f"Закрытых сделок: {len(closed)}",
        f"Плюсовых: {len(wins)} | Минусовых: {len(losses)}",
        f"Винрейт: {winrate:.1f}%",
        f"Последний тик: {lab.get('last_tick') or 'нет'}",
    ]

    if active:
        trailing_activated = active.get("trailing_activated", False)
        trailing_status = "активен" if trailing_activated else "ждёт"
        trailing_stop = float(active.get("trailing_stop") or 0)
        lines += [
            "",
            "Активная сделка:",
            f"Пара: {active['pair']}",
            f"Вход: {active['entry_price']}",
            f"Score: {active.get('entry_score', 0)}",
            f"Откат при входе: {float(active.get('entry_pullback_pct') or 0):+.2f}%",
            f"ATR: {float(active.get('atr') or 0):.6f}",
            f"Трейлинг: {trailing_status} | стоп {trailing_stop:.6f}",
            f"Открыта: {active['opened_at']}",
        ]
    else:
        lines += ["", "Активная сделка: нет"]

    return "\n".join(lines)


def rotation_market_report() -> str:
    rows = analyze_rotation_market()

    if not rows:
        return "🧪 Rotation Market: нет данных по парам."

    lines = ["🧪 ROTATION MARKET", ""]

    for row in rows:
        if row.get("error"):
            lines.append(f"⚠️ {row.get('pair')}: {row.get('error')}")
            continue

        lines.append(
            f"{row['pair']} | score {row['score']} | "
            f"pullback {float(row.get('pullback_pct', 0)):+.2f}% | "
            f"uptrend {'да' if row.get('uptrend') else 'нет'} | "
            f"vol x{float(row.get('volume_ratio', 1)):.2f}"
        )

    return "\n".join(lines)


def rotation_history(limit: int = 20) -> str:
    lab = load_rotation_state()
    journal = lab.get("journal") or []
    rows = journal[-limit:]

    if not rows:
        return "История Rotation Lab пока пустая."

    lines = ["📜 ROTATION HISTORY", ""]

    for row in rows:
        action = row.get("action")
        pair = row.get("pair")

        if action == "OPEN":
            lines.append(
                f"🟢 OPEN {pair} | price={row.get('price')} | {row.get('note')}"
            )
        elif action == "CLOSE":
            lines.append(
                f"🔴 CLOSE {pair} | result={float(row.get('result_pct') or 0):+.2f}% | "
                f"BTC {float(row.get('btc_before') or 0):.8f} → {float(row.get('btc_after') or 0):.8f} | "
                f"{row.get('reason')}"
            )

    return "\n".join(lines)
