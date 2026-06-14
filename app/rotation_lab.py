from __future__ import annotations

import json
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

TIMEFRAME = "5m"
CANDLE_LIMIT = 60

VIRTUAL_BTC_START = 1.0
ENTRY_STRENGTH_PCT = 0.6
TAKE_PROFIT_PCT = 1.0
STOP_LOSS_PCT = -0.6
MIN_VOLUME_GROWTH = 1.2
MIN_ENTRY_SCORE = 60
MAX_JOURNAL_ROWS = 1000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    # мягкая миграция старого состояния
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
    """Анализ BTC-пары по свечам.

    Цель: найти монету, которая сейчас усиливается относительно BTC.
    Пока это только лабораторный сигнал, без реальных ордеров.
    """
    candles = exchange.fetch_ohlcv(pair, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)

    if not candles or len(candles) < 40:
        return None

    # candle: [timestamp, open, high, low, close, volume]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]

    current = closes[-1]
    prev_1h = closes[-12]   # 12 свечей * 5m = 1 час
    prev_3h = closes[-36]   # 36 свечей * 5m = 3 часа

    change_1h = (current / prev_1h - 1) * 100 if prev_1h else 0.0
    change_3h = (current / prev_3h - 1) * 100 if prev_3h else 0.0

    avg_volume = sum(volumes[-30:-5]) / max(len(volumes[-30:-5]), 1)
    recent_volume = sum(volumes[-5:]) / max(len(volumes[-5:]), 1)
    volume_growth = recent_volume / avg_volume if avg_volume else 0.0

    score = 0
    reasons: list[str] = []

    if change_1h > 0:
        score += 20
        reasons.append("1h positive")

    if change_1h >= ENTRY_STRENGTH_PCT:
        score += 30
        reasons.append("1h impulse")

    if change_3h > 0:
        score += 20
        reasons.append("3h positive")

    if volume_growth >= MIN_VOLUME_GROWTH:
        score += 20
        reasons.append("volume growth")

    if current > closes[-3] > closes[-6]:
        score += 10
        reasons.append("short acceleration")

    return {
        "pair": pair,
        "price": current,
        "change_1h": change_1h,
        "change_3h": change_3h,
        "volume_growth": volume_growth,
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
            rows.append({
                "pair": pair,
                "error": str(exc),
                "score": 0,
            })

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
        and float(best.get("change_1h") or 0) >= ENTRY_STRENGTH_PCT
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

    trade = {
        "pair": pair_data["pair"],
        "entry_price": float(pair_data["price"]),
        "entry_strength_pct": float(pair_data.get("change_1h") or 0),
        "entry_score": int(pair_data.get("score") or 0),
        "entry_change_3h": float(pair_data.get("change_3h") or 0),
        "entry_volume_growth": float(pair_data.get("volume_growth") or 0),
        "entry_reasons": pair_data.get("reasons") or [],
        "btc_amount": btc_before,
        "opened_at": now_iso(),
    }

    lab["active_trade"] = trade

    _append_journal(lab, {
        "time": now_iso(),
        "action": "OPEN",
        "pair": trade["pair"],
        "price": trade["entry_price"],
        "btc_before": btc_before,
        "btc_after": btc_before,
        "score": trade["entry_score"],
        "change_1h": trade["entry_strength_pct"],
        "change_3h": trade["entry_change_3h"],
        "volume_growth": trade["entry_volume_growth"],
        "note": (
            f"score={trade['entry_score']} | "
            f"1h={trade['entry_strength_pct']:+.2f}% | "
            f"3h={trade['entry_change_3h']:+.2f}% | "
            f"vol x{trade['entry_volume_growth']:.2f}"
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
        result_pct = (current_price / entry_price - 1) * 100 if entry_price else 0.0

        if result_pct >= TAKE_PROFIT_PCT:
            lab = close_virtual_trade(lab, current_price, "take_profit")
            save_rotation_state(lab)
            return {
                "status": "closed",
                "reason": "take_profit",
                "pair": pair,
                "result_pct": result_pct,
                "virtual_btc": float(lab["virtual_btc"]),
                "event": lab.get("last_event"),
            }

        if result_pct <= STOP_LOSS_PCT:
            lab = close_virtual_trade(lab, current_price, "stop_loss")
            save_rotation_state(lab)
            return {
                "status": "closed",
                "reason": "stop_loss",
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
            "change_1h": best.get("change_1h"),
            "change_3h": best.get("change_3h"),
            "volume_growth": best.get("volume_growth"),
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
            f"Score: {event.get('score')}\n"
            f"1h: {float(event.get('change_1h') or 0):+.2f}%\n"
            f"3h: {float(event.get('change_3h') or 0):+.2f}%\n"
            f"Объём: x{float(event.get('volume_growth') or 0):.2f}\n"
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
    active = lab.get("active_trade")

    lines = [
        "🧪 ROTATION LAB",
        "",
        f"Режим: {lab.get('mode')}",
        f"Включён: {'да' if lab.get('enabled', True) else 'нет'}",
        f"База: {lab.get('base')}",
        f"Пары: {', '.join(load_rotation_pairs('BTC'))}",
        f"BTC старт: {initial:.8f}",
        f"BTC сейчас: {current:.8f}",
        f"Результат: {growth_pct:+.2f}%",
        "",
        f"Закрытых сделок: {len(closed)}",
        f"Плюсовых: {len(wins)}",
        f"Минусовых: {len(losses)}",
        f"Последний тик: {lab.get('last_tick') or 'нет'}",
    ]

    if active:
        lines += [
            "",
            "Активная сделка:",
            f"Пара: {active['pair']}",
            f"Вход: {active['entry_price']}",
            f"Score: {active.get('entry_score', 0)}",
            f"1h: {float(active.get('entry_strength_pct') or 0):+.2f}%",
            f"3h: {float(active.get('entry_change_3h') or 0):+.2f}%",
            f"Объём: x{float(active.get('entry_volume_growth') or 0):.2f}",
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
            f"1h {float(row['change_1h']):+.2f}% | "
            f"3h {float(row['change_3h']):+.2f}% | "
            f"vol x{float(row['volume_growth']):.2f}"
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
