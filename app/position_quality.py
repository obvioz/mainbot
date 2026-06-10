from __future__ import annotations

from app.strategy_params import get_tp_levels
from app.config import CATEGORIES, TAKE_PROFIT_LEVELS, ENTRY_STEPS
from app.formatters import fmt_usdt, fmt_money


def position_quality_score(coin: str, pos: dict, item: dict | None, signal: dict | None, market_context: dict | None) -> dict:
    """Оценка качества уже открытой позиции.

    Это не прогноз цены. Задача — понять, что делать с текущей позицией:
    держать, добирать, фиксировать часть или не докупать.
    """
    coin = coin.upper().strip()
    category = CATEGORIES.get(coin, "STRONG_ALT")
    current = float((item or {}).get("current", 0) or 0)
    avg = float(pos.get("avg_price", 0) or 0)
    entry_count = int(pos.get("entry_count", 0) or 0)
    max_entries = len(ENTRY_STEPS)
    next_buy = float(pos.get("next_buy_price", 0) or 0)
    invested = float(pos.get("invested_usdt", 0) or 0)
    qty = float(pos.get("qty", 0) or 0)
    value = qty * current if current else 0.0
    pnl_pct = (current / avg - 1) * 100 if current and avg else 0.0
    pnl_usdt = value - invested if value else 0.0

    score = 50
    reasons: list[str] = []

    market_score = int((market_context or {}).get("score", 50) or 50)
    market_mode = (market_context or {}).get("mode", "UNKNOWN")
    if market_score >= 70:
        score += 10; reasons.append("рынок поддерживает удержание")
    elif market_score >= 50:
        score += 3; reasons.append("рынок нейтральный")
    elif market_score >= 35:
        score -= 10; reasons.append("рынок осторожный")
    else:
        score -= 20; reasons.append("рынок опасный")

    if signal:
        strength = signal.get("strength_vs_btc", {}) or {}
        if strength.get("state") == "STRONG":
            score += 12; reasons.append("монета сильнее BTC")
        elif strength.get("state") == "WEAK":
            score -= 14; reasons.append("монета слабее BTC")

        deriv = signal.get("derivatives") or {}
        if deriv.get("funding_state") in {"OVERHEATED", "HOT"}:
            score -= 8; reasons.append("funding перегрет")
        elif deriv.get("funding_state") in {"NEUTRAL", "COOL"}:
            score += 3
        if deriv.get("oi_state") == "EXPANDING_FAST":
            score -= 8; reasons.append("OI растет слишком быстро")
        elif deriv.get("oi_state") == "FLUSHED":
            score += 5; reasons.append("OI очищен после пролива")

        news = signal.get("news_risk") or {}
        if news.get("state") == "HIGH":
            score -= 20; reasons.append("высокий новостной риск")
        elif news.get("state") == "ELEVATED":
            score -= 10; reasons.append("повышенный новостной риск")
        elif news.get("state") == "LOW":
            score += 4

    if pnl_pct >= 30:
        score += 4; reasons.append("позиция в сильном плюсе")
    elif pnl_pct >= 10:
        score += 2; reasons.append("позиция в плюсе")
    elif pnl_pct <= -25:
        score -= 8; reasons.append("глубокая просадка позиции")
    elif pnl_pct <= -10:
        score -= 3; reasons.append("позиция в минусе")

    if entry_count >= max_entries:
        score -= 12; reasons.append("лимит входов уже использован")
    elif next_buy and current and current <= next_buy:
        score += 7; reasons.append("цена дошла до зоны добора")

    score = max(0, min(100, int(round(score))))

    # Рекомендация по действию.
    tp_levels = get_tp_levels(coin)
    tp1, tp2, tp3 = tp_levels[:3]
    if pnl_pct >= tp3:
        recommendation = "TAKE_PROFIT_STRONG"
        action_text = f"сильная зона фиксации: можно закрыть 50–75% или вести остаток трейлингом"
    elif pnl_pct >= tp2:
        recommendation = "TAKE_PROFIT_2"
        action_text = f"вторая зона фиксации: можно продать 25–50%"
    elif pnl_pct >= tp1:
        if score >= 75 and market_score >= 60:
            recommendation = "TAKE_PROFIT_SOFT"
            action_text = "первая зона фиксации, но позиция сильная: можно продать 20–25% и оставить хвост"
        else:
            recommendation = "TAKE_PROFIT_1"
            action_text = "первая зона фиксации: можно продать 25–40%"
    elif next_buy and current and current <= next_buy:
        if score >= 65 and entry_count < max_entries:
            recommendation = "ADD_ALLOWED"
            action_text = "добор разрешен, но только по фактической цене и малой частью"
        else:
            recommendation = "WAIT_NO_ADD"
            action_text = "цена в зоне добора, но качество позиции слабое — лучше не спешить"
    elif score < 45:
        recommendation = "REDUCE_RISK"
        action_text = "не докупать; при отскоке можно снижать риск"
    else:
        recommendation = "HOLD"
        action_text = "держать и ждать следующего уровня"

    return {
        "coin": coin,
        "category": category,
        "score": score,
        "recommendation": recommendation,
        "action_text": action_text,
        "reasons": reasons[:5],
        "current": current,
        "avg": avg,
        "pnl_pct": pnl_pct,
        "pnl_usdt": pnl_usdt,
        "entry_count": entry_count,
        "max_entries": max_entries,
        "next_buy": next_buy,
        "market_mode": market_mode,
        "market_score": market_score,
        "tp_levels": tp_levels,
    }


def format_position_quality(q: dict) -> str:
    coin = q["coin"]
    score = q["score"]
    emoji = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"
    lines = [
        f"{emoji} {coin} — Quality {score}/100",
        f"Средняя: {fmt_usdt(q['avg'])} | Сейчас: {fmt_usdt(q['current'])}",
        f"PnL: {q['pnl_pct']:+.2f}% / {fmt_money(q['pnl_usdt'])}",
        f"Входов: {q['entry_count']}/{q['max_entries']} | след. добор: {fmt_usdt(q['next_buy'])}",
        f"Решение: {q['action_text']}",
    ]
    if q.get("reasons"):
        lines.append("Причины: " + "; ".join(q["reasons"][:3]))
    return "\n".join(lines)
