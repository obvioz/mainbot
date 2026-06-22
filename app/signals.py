from app.config import BTC_PANIC_24H, ALLOCATION_USDT, ENTRY_STEPS, CATEGORIES
from app.risk_manager import get_coin_risk
from app.storage import get_position
from app.formatters import fmt_usdt, fmt_money, allocation_left_text
from app.ui import pct_emoji, status_emoji, hr
from app.market_intelligence import build_market_context, coin_strength_vs_btc
from app.derivatives import short_derivatives_text
from app.news_risk import short_news_text
from app.position_quality import position_quality_score
from app.strategy_robustness import get_reliability_score
from app.volatility_profile import (
    get_profile,
    get_entry_levels_for_coin,   # единый источник DCA-уровней (signals + storage)
    FALLBACK_ENTRY_LEVELS,
)


# Режим EXPANDING поднимает планку score (не блокирует вход) — специфично для сигнала.
EXPANDING_SCORE_BUMP = 10


def get_market_regime(btc_item: dict | None, items: list[dict] | None = None, market_context: dict | None = None) -> dict:
    """Совместимость со старыми вызовами + новый Market Intelligence."""
    if market_context:
        return {
            "name": market_context.get("mode", "UNKNOWN"),
            "score": market_context.get("score", 50),
            "comment": market_context.get("recommendation", ""),
            "context": market_context,
        }
    if items:
        ctx = build_market_context(items)
        return {"name": ctx["mode"], "score": ctx["score"], "comment": ctx["recommendation"], "context": ctx}

    if not btc_item or "error" in btc_item:
        return {"name": "UNKNOWN", "score": 50, "comment": "нет данных по BTC"}
    ch24 = float(btc_item.get("change_24h", 0))
    ch7 = float(btc_item.get("change_7d", 0))
    dd = float(btc_item.get("drawdown_30d_high", 0))
    atr = float(btc_item.get("atr_pct", 0))
    score = 50
    if ch7 > 5: score += 15
    elif ch7 < -8: score -= 15
    if ch24 <= BTC_PANIC_24H: score -= 25
    elif ch24 < -3: score -= 10
    elif ch24 > 2: score += 5
    if dd <= -18: score -= 15
    elif dd <= -10: score -= 5
    elif dd > -5: score += 10
    if atr >= 6: score -= 10
    elif atr <= 3: score += 5
    score = max(0, min(100, score))
    if score >= 75:
        return {"name":"BULL", "score":score, "comment":"BTC выглядит сильным, просадки по альтам можно рассматривать активнее."}
    if score >= 60:
        return {"name":"HEALTHY CORRECTION", "score":score, "comment":"Рынок не сломан, коррекции можно отбирать."}
    if score >= 45:
        return {"name":"SIDEWAYS", "score":score, "comment":"Рынок нейтральный, лучше входить частями и не спешить."}
    if score >= 30:
        return {"name":"RISK-OFF", "score":score, "comment":"BTC слабый, по альтам нужен осторожный режим."}
    return {"name":"BEAR / PANIC", "score":score, "comment":"Рынок опасный, новые покупки только минимальными частями."}


def _coin_bucket(coin: str) -> str:
    if coin in {"BTC", "ETH"}:
        return "CORE"
    if coin in {"BNB", "SOL", "LINK", "AAVE"}:
        return "QUALITY_ALT"
    if coin in {"AVAX", "TON", "SUI", "NEAR"}:
        return "ALT"
    return "NARRATIVE"


def _price_attractiveness(coin: str, dd30: float, ch7: float, atr: float, first_level: float) -> dict:
    """Оценивает не силу монеты, а насколько цена вообще интересна для входа.

    Главный принцип v24: сила без скидки = WATCH, а не покупка.
    """
    bucket = _coin_bucket(coin)
    abs_dd = abs(min(dd30, 0.0))

    if bucket == "CORE":
        min_entry = max(first_level * 0.85, 6.0)
        good = max(first_level * 1.45, 11.0)
        great = max(first_level * 2.2, 17.0)
    elif bucket == "QUALITY_ALT":
        min_entry = max(first_level * 0.95, 10.0)
        good = max(first_level * 1.5, 17.0)
        great = max(first_level * 2.3, 27.0)
    elif bucket == "ALT":
        min_entry = max(first_level, 12.0)
        good = max(first_level * 1.65, 20.0)
        great = max(first_level * 2.5, 32.0)
    else:
        min_entry = max(first_level, 15.0)
        good = max(first_level * 1.7, 26.0)
        great = max(first_level * 2.6, 42.0)

    # Если монета за неделю уже растет, а от high почти не отошла — это не dip.
    near_high_and_green = abs_dd < min_entry and ch7 > 2.0

    if near_high_and_green:
        score = 8
        state = "EXPENSIVE"
        comment = "монета сильная, но цена неинтересная: близко к 30д high и уже росла за 7д"
    elif abs_dd >= great:
        score = 92
        state = "GREAT"
        comment = f"цена в глубокой зоне интереса: просадка {dd30:+.1f}%"
    elif abs_dd >= good:
        score = 78
        state = "GOOD"
        comment = f"цена в хорошей зоне интереса: просадка {dd30:+.1f}%"
    elif abs_dd >= min_entry:
        score = 62
        state = "FAIR"
        comment = f"цена дошла до минимальной зоны интереса: просадка {dd30:+.1f}%"
    elif ch7 < 0 and abs(ch7) >= max(atr * 1.8, min_entry * 0.6):
        score = 48
        state = "EARLY_DIP"
        comment = "есть свежая 7д коррекция, но 30д скидка еще слабая"
    else:
        score = 20
        state = "NOT_INTERESTING"
        comment = "цена пока не дает нормальной скидки для нашей стратегии"

    return {
        "score": int(score),
        "state": state,
        "comment": comment,
        "min_entry_dd": float(min_entry),
        "good_dd": float(good),
        "great_dd": float(great),
    }


def _risk_state(derivatives: dict, news_risk: dict, btc_weak: bool, coin: str) -> dict:
    score = 75
    reasons = []
    hard_avoid = False

    if news_risk:
        state = news_risk.get("state")
        if state == "HIGH":
            # News-риск пока ненадёжен (keyword-substring матчинг даёт ложные HIGH
            # даже по BTC). Поэтому он только штрафует score, но НЕ блокирует вход.
            # Жёсткий запрет здоровья монеты делает coin_health.health_check по данным биржи.
            score -= 45; reasons.append(news_risk.get("comment", "высокий новостной риск"))
        elif state == "ELEVATED":
            score -= 22; reasons.append(news_risk.get("comment", "повышенный новостной риск"))
        elif state == "LOW":
            score += 4; reasons.append("новостной фон без явного негатива")

    if derivatives:
        f_state = derivatives.get("funding_state")
        oi_state = derivatives.get("oi_state")
        score += int(derivatives.get("funding_score_adj", 0) or 0)
        score += int(derivatives.get("oi_score_adj", 0) or 0)
        if f_state == "OVERHEATED":
            reasons.append(derivatives.get("funding_comment", "funding перегрет"))
        if oi_state == "EXPANDING_FAST":
            reasons.append(derivatives.get("oi_comment", "OI резко растет"))
        if f_state == "OVERHEATED" and oi_state == "EXPANDING_FAST":
            hard_avoid = True

    if btc_weak and coin != "BTC":
        score -= 18
        reasons.append("BTC падает слишком сильно — альты повышенного риска")

    return {"score": max(0, min(100, int(score))), "reasons": reasons, "hard_avoid": hard_avoid}


def classify_signal(item: dict, btc_item: dict | None = None, market_context: dict | None = None) -> dict:
    if "error" in item:
        return {"status":"ERROR", "reason":item["error"], "entry_usdt":0, "score":0, "reasons":[item["error"]]}

    coin = item["coin"]
    ch24 = float(item["change_24h"])
    ch7 = float(item["change_7d"])
    dd30 = float(item["drawdown_30d_high"])
    atr = float(item.get("atr_pct", 0) or 0)
    vclass = item.get("volatility_class", "MEDIUM")
    # Персональные ATR-уровни входа из профиля волатильности (с защитными границами)
    # вместо прежних фиксированных dca_levels монеты.
    vol_levels = get_entry_levels_for_coin(coin)
    entry_levels = vol_levels["levels"]
    first_level = float(entry_levels[0])

    market_score = int(market_context.get("score", 50)) if market_context else 50
    market_mode = market_context.get("mode", "UNKNOWN") if market_context else "UNKNOWN"
    btc_weak = bool(btc_item and "error" not in btc_item and float(btc_item["change_24h"]) <= BTC_PANIC_24H)
    strength = coin_strength_vs_btc(item, btc_item)
    derivatives = item.get("derivatives") or {}
    news_risk = item.get("news_risk") or {}
    reliability = get_reliability_score(coin)
    pos = get_position(coin)

    bucket = _coin_bucket(coin)
    price = _price_attractiveness(coin, dd30, ch7, atr, first_level)
    risk = _risk_state(derivatives, news_risk, btc_weak, coin)

    reasons = []
    reasons.append(price["comment"])

    # Strength is useful only AFTER price is acceptable. It should not create a buy near highs.
    strength_score = 50
    if coin == "BTC":
        strength_score = 65
    elif strength["state"] == "STRONG":
        strength_score = 85
        reasons.append(f"сильнее BTC за 7д на {strength['delta_7d']:+.1f}%")
    elif strength["state"] == "WEAK":
        strength_score = 25
        reasons.append(f"слабее BTC за 7д на {strength['delta_7d']:+.1f}%")
    else:
        strength_score = 55

    reliability_score = 55 if reliability is None else float(reliability)
    if reliability is not None:
        if reliability >= 80:
            reasons.append(f"историческая устойчивость высокая ({reliability:.0f}/100)")
        elif reliability < 55:
            reasons.append(f"историческая устойчивость слабая/средняя ({reliability:.0f}/100)")

    # Market score should affect size and caution, but not blindly kill BTC/ETH in fear.
    if market_score < 35:
        if bucket == "CORE":
            market_component = 45
            reasons.append("опасный рынок: для BTC/ETH только малый набор")
        elif bucket == "QUALITY_ALT":
            market_component = 35
            reasons.append("опасный рынок: по сильным альтам нужен дисконт")
        else:
            market_component = 20
            reasons.append("опасный рынок: слабые/рисковые альты лучше избегать")
    elif market_score < 50:
        market_component = 45
        reasons.append("рынок осторожный: входы уменьшать")
    elif market_score < 65:
        market_component = 58
        reasons.append("рынок нейтральный")
    else:
        market_component = 72
        reasons.append("рынок допускает покупки")

    # Weighted decision score. Price attractiveness is dominant.
    score = (
        price["score"] * 0.38 +
        strength_score * 0.18 +
        risk["score"] * 0.17 +
        market_component * 0.15 +
        reliability_score * 0.12
    )

    # Penalize high volatility for risky buckets, but not too much for planned DCA.
    if vclass == "EXTREME":
        score -= 8; reasons.append("экстремальная волатильность: входы только глубже")
    elif vclass == "HIGH":
        score -= 4; reasons.append("высокая волатильность: входы разносить шире")

    # Режим волатильности (короткий vs длинный ATR). EXPANDING не блокирует вход
    # (жёсткую защиту от аномалии делает coin_health), но поднимает планку score —
    # фактически требует на +10 более уверенный сигнал. QUIET/NORMAL без изменений.
    if vol_levels.get("vol_regime") == "EXPANDING":
        score -= EXPANDING_SCORE_BUMP
        reasons.append("⚡ повышенная волатильность (режим EXPANDING): беру только уверенные входы")
    if vol_levels.get("profile_missing"):
        reasons.append("профиль волатильности отсутствует — уровни входа по дефолту")
    elif vol_levels.get("clamped"):
        reasons.append("ATR-уровни входа подрезаны под защитные границы (-2% / -45%)")

    # Portfolio-aware additions.
    coin_risk = get_coin_risk(coin)
    if coin_risk.get("soft_limit") and coin_risk.get("invested", 0) > coin_risk.get("soft_limit", 0):
        score -= 6; reasons.append("позиция выше мягкого ориентира")

    if pos:
        entry_count = int(pos.get("entry_count", 0) or 0)
        next_buy = float(pos.get("next_buy_price", 0) or 0)
        if entry_count >= len(ENTRY_STEPS):
            score -= 30; reasons.append("достигнут лимит входов по монете")
        elif next_buy and float(item["current"]) <= next_buy:
            score += 18; reasons.append("цена дошла до следующего уровня добора")

    # Risk reasons appended after key reasons so they don't dominate the first line.
    for rr in risk["reasons"]:
        if rr not in reasons:
            reasons.append(rr)

    score = max(0, min(100, int(round(score))))

    # Gating: no first buy if the price is not interesting enough.
    price_ok_for_first = price["state"] in {"FAIR", "GOOD", "GREAT"}
    price_good = price["state"] in {"GOOD", "GREAT"}
    price_not_interesting = price["state"] in {"EXPENSIVE", "NOT_INTERESTING"}

    hard_avoid = bool(risk["hard_avoid"])
    if market_score < 35 and bucket in {"ALT", "NARRATIVE"} and price["state"] not in {"GREAT"}:
        hard_avoid = True

    if pos:
        # For open positions, status reflects whether we should add/hold/reduce.
        next_buy = float(pos.get("next_buy_price", 0) or 0)
        current = float(item.get("current", 0) or 0)
        avg = float(pos.get("avg_price", 0) or 0)
        pnl = (current / avg - 1) * 100 if avg else 0
        if pnl >= 10:
            status = "ACCUMULATION"  # UI action will say take profit, not buy.
        elif next_buy and current <= next_buy and not hard_avoid and risk["score"] >= 45:
            status = "ACCUMULATION"
        elif hard_avoid or score < 38:
            status = "AVOID"
        else:
            status = "WATCH"
    else:
        if hard_avoid:
            status = "AVOID"
        elif price_not_interesting:
            status = "WATCH"
            reasons.insert(0, "покупка не рекомендуется: нет скидки")
        elif score >= 82 and price_good and risk["score"] >= 55:
            status = "STRONG_BUY"
        elif score >= 58 and price_ok_for_first and risk["score"] >= 45:
            status = "ACCUMULATION"
        elif bucket == "CORE" and price_ok_for_first and risk["score"] >= 45 and score >= 50:
            status = "ACCUMULATION"
        elif bucket == "QUALITY_ALT" and price_good and strength_score >= 50 and risk["score"] >= 55 and score >= 52:
            status = "ACCUMULATION"
        elif score >= 36:
            status = "WATCH"
        else:
            status = "AVOID"

    entry_usdt = 0.0
    if status in {"STRONG_BUY", "ACCUMULATION"} and not pos:
        max_alloc = ALLOCATION_USDT.get(coin, 0)
        step_index = 0
        multiplier = 1.0
        if market_score < 35:
            multiplier = 0.25 if bucket == "CORE" else 0.12
        elif market_score < 50:
            multiplier = 0.45
        elif market_score < 65:
            multiplier = 0.70
        if price["state"] == "FAIR":
            multiplier *= 0.65
        elif price["state"] == "GREAT":
            multiplier *= 1.15
        entry_usdt = round(max_alloc * ENTRY_STEPS[step_index] * multiplier, 2)
    elif status in {"STRONG_BUY", "ACCUMULATION"} and pos:
        # Existing position: no suggested amount here; Bybit monitor/position action handles it.
        entry_usdt = 0.0

    return {
        "status": status,
        "reason": "; ".join(reasons) if reasons else "нет сильного сигнала",
        "reasons": reasons or ["нет сильного сигнала"],
        "entry_usdt": entry_usdt,
        "score": score,
        "market_mode": market_mode,
        "market_score": market_score,
        "strength_vs_btc": strength,
        "derivatives": derivatives,
        "news_risk": news_risk,
        "reliability": reliability,
        "price_attractiveness": price,
        "risk_state": risk,
        "vol_levels": vol_levels,
    }


def _human_market_mode(mode: str) -> str:
    mapping = {
        "BULL": "рынок сильный",
        "HEALTHY CORRECTION": "здоровая коррекция",
        "SIDEWAYS": "боковик",
        "RISK-OFF": "осторожный режим",
        "BEAR / PANIC": "опасный режим",
    }
    return mapping.get(mode or "UNKNOWN", mode or "UNKNOWN")


def _short_risk_text(market_context: dict) -> str:
    score = int(market_context.get("score", 50))
    mode = market_context.get("mode", "UNKNOWN")
    if score >= 70:
        return "Можно искать покупки, но входить частями."
    if score >= 50:
        return "Покупки только по сильным монетам и без спешки."
    if score >= 35:
        return "Новые покупки лучше уменьшать, альты осторожно."
    return "Рынок опасный: агрессивные доборы запрещены."


def _next_dca_text_for_position(item: dict, pos: dict | None) -> str:
    if not pos:
        return ""
    current = float(item.get("current", 0) or 0)
    next_buy = float(pos.get("next_buy_price", 0) or 0)
    if not current or not next_buy:
        return ""
    diff = (current / next_buy - 1) * 100
    if current <= next_buy:
        return f"🟢 Добор уже в зоне: {fmt_usdt(next_buy)} USDT"
    return f"До следующего добора: {diff:.1f}% вниз → {fmt_usdt(next_buy)} USDT"


def _take_profit_text_for_position(item: dict, pos: dict | None) -> str:
    if not pos:
        return ""
    current = float(item.get("current", 0) or 0)
    avg = float(pos.get("avg_price", 0) or 0)
    if not current or not avg:
        return ""
    pnl = (current / avg - 1) * 100
    if pnl >= 15:
        return f"🟢 Позиция в плюсе {pnl:+.1f}% — можно фиксировать часть."
    if pnl >= 10:
        return f"🟢 Позиция в плюсе {pnl:+.1f}% — первая зона фиксации."
    if pnl > 0:
        return f"Позиция почти в зоне фиксации: {pnl:+.1f}%"
    return f"Позиция пока в минусе: {pnl:+.1f}%"


def _buy_zones_from_current(item: dict) -> str:
    levels = item.get("dca_levels") or []
    price = float(item.get("current", 0) or 0)
    if len(levels) < 4 or not price:
        return ""
    prices = [price * (1 - float(x)/100) for x in levels[:4]]
    return (
        "🎯 Зоны интереса от текущей цены:\n"
        f"• первая: {fmt_usdt(prices[0])}  • хорошая: {fmt_usdt(prices[1])}\n"
        f"• сильная: {fmt_usdt(prices[2])}  • паника: {fmt_usdt(prices[3])}"
    )


def _decision_action_text(item: dict, signal: dict, pos: dict | None) -> str:
    status = signal.get("status", "WATCH")
    coin = item.get("coin", "")
    current = float(item.get("current", 0) or 0)
    if pos:
        next_buy = float(pos.get("next_buy_price", 0) or 0)
        avg = float(pos.get("avg_price", 0) or 0)
        pnl = (current / avg - 1) * 100 if avg else 0
        if pnl >= 15:
            return "фиксировать часть прибыли"
        if pnl >= 10:
            return "первая фиксация прибыли"
        if next_buy and current <= next_buy:
            return "разрешен добор по лесенке"
        if signal.get("score", 0) < 45:
            return "не докупать, ждать"
        return "держать позицию"

    if status == "STRONG_BUY":
        return "можно открыть первый вход"
    if status == "ACCUMULATION":
        return "малый первый вход только при подтверждении"
    if status == "WATCH":
        return "ждать лучшую цену"
    return "не входить"


def _signal_title(status: str) -> str:
    if status == "STRONG_BUY":
        return "🟢 STRONG BUY"
    if status == "ACCUMULATION":
        return "🟡 ACCUMULATION"
    if status == "WATCH":
        return "⚪ WATCH"
    if status == "AVOID":
        return "🔴 AVOID"
    return "⚪ WATCH"


def _vol_levels_line(signal: dict) -> str:
    """Строка с персональными уровнями входа от волатильности монеты."""
    vl = signal.get("vol_levels") or {}
    levels = vl.get("levels") or []
    if len(levels) < 3:
        return ""
    if vl.get("profile_missing"):
        head = "Уровни входа (профиль отсутствует, дефолт)"
    else:
        head = f"Уровни от волатильности (класс {vl.get('vol_class')})"
        if vl.get("vol_regime") == "EXPANDING":
            head += " ⚡"
    return f"{head}: т1 -{levels[0]:g}% / т2 -{levels[1]:g}% / т3 -{levels[2]:g}%"


def _next_level_text_from_vol(signal: dict, current: float) -> str:
    """Превью следующего уровня входа от текущей цены по персональным ATR-уровням."""
    vl = signal.get("vol_levels") or {}
    levels = vl.get("levels") or []
    if len(levels) >= 2 and current:
        level_pct = float(levels[1])
        next_price = current * (1 - level_pct / 100)
        return f"${fmt_usdt(next_price)} (-{level_pct:g}%)"
    return "нет данных"


def _signal_card(item: dict, signal: dict, detailed: bool = True) -> str:
    coin = item["coin"]
    pos = get_position(coin)
    strength = signal.get("strength_vs_btc", {})
    current = float(item.get("current", 0) or 0)
    dd30 = float(item.get("drawdown_30d_high", 0) or 0)
    ch7 = float(item.get("change_7d", 0) or 0)
    vclass = item.get("volatility_class", "?")
    score = int(signal.get("score", 0))
    status = signal.get("status", "NO_SIGNAL")

    action = _decision_action_text(item, signal, pos)
    lines = [
        f"{_signal_title(status)} — {coin}",
        f"Действие: {action}",
        f"Цена: {fmt_usdt(current)} USDT | Score {score}/100",
        f"Просадка от 30д high: {dd30:+.1f}% | 7д: {ch7:+.1f}%",
        f"Цена входа: {(signal.get('price_attractiveness') or {}).get('state','?')}",
        f"Волатильность: {vclass}",
    ]

    vol_line = _vol_levels_line(signal)
    if vol_line:
        lines.append(vol_line)

    deriv = signal.get("derivatives") or item.get("derivatives") or {}
    news = signal.get("news_risk") or item.get("news_risk") or {}
    if deriv and deriv.get("funding_state") == "OVERHEATED":
        f_pct = deriv.get("funding_pct")
        f_text = f"{float(f_pct):+.3f}%" if f_pct is not None else "?"
        lines.append(f"⚠️ Funding OVERHEATED: {f_text}")
    if news and news.get("state", "UNKNOWN") != "UNKNOWN":
        lines.append(f"News Risk: {news.get('state')} ({news.get('risk_score', 0)}/100)")
    if coin != "BTC" and strength.get("state", "NEUTRAL") != "NEUTRAL":
        lines.append(f"Сила к BTC: {strength.get('state','?')} ({strength.get('delta_7d',0):+.1f}%)")

    if pos:
        avg = float(pos.get("avg_price", 0) or 0)
        invested = float(pos.get("invested_usdt", 0) or 0)
        entries = int(pos.get("entry_count", 0) or 0)
        pnl = (current / avg - 1) * 100 if avg else 0
        q = position_quality_score(coin, pos, item, signal, {"score": signal.get("market_score", 50), "mode": signal.get("market_mode", "UNKNOWN")})
        lines.extend([
            "",
            f"💼 У тебя уже {entries} вход(а)",
            f"Средняя: {fmt_usdt(avg)} | PnL {pnl:+.1f}%",
            f"Вложено: {fmt_money(invested)}",
            f"Quality: {q['score']}/100 — {q['action_text']}",
        ])
        dca = _next_dca_text_for_position(item, pos)
        tp = _take_profit_text_for_position(item, pos)
        if dca:
            lines.append(dca)
        if tp:
            lines.append(tp)
    else:
        entry = float(signal.get("entry_usdt", 0) or 0)
        if entry:
            lines.append(f"Размер пробного входа: {fmt_money(entry)}")

    reasons = signal.get("reasons", [])[:3]
    if reasons:
        lines.append("")
        lines.append("Почему:")
        for r in reasons:
            lines.append(f"• {r}")

    return "\n".join(lines)


def _watch_line(item: dict, signal: dict) -> str:
    coin = item["coin"]
    pos = get_position(coin)
    prefix = "💼" if pos else "•"
    reason = (signal.get("reasons") or ["ждем лучшую цену"])[0]
    if len(reason) > 58:
        reason = reason[:55] + "…"
    dd30 = float(item.get("drawdown_30d_high", 0) or 0)
    score = int(signal.get("score", 0))
    if pos:
        dca = _next_dca_text_for_position(item, pos)
        return f"{prefix} {coin}: {fmt_usdt(item['current'])} | Score {score} | {dca}"
    return f"{prefix} {coin}: {fmt_usdt(item['current'])} | {dd30:+.1f}% от high | {reason}"


def _market_header_clean(market_context: dict, btc: dict | None) -> str:
    fng = market_context.get("fear_greed", {})
    dom = market_context.get("btc_dominance", {})
    trend = market_context.get("btc_trend", {})
    alt = market_context.get("alt_basket", {})
    mode = market_context.get("mode", "UNKNOWN")
    score = market_context.get("score", 50)

    btc_line = "BTC: нет данных"
    if btc and "error" not in btc:
        btc_line = f"BTC: {fmt_usdt(btc['current'])} USDT | 24ч {btc['change_24h']:+.1f}% | 7д {btc['change_7d']:+.1f}%"

    dom_val = dom.get("value")
    dom_text = f"{float(dom_val):.1f}%" if dom_val else "нет данных"
    alt_text = "альты слабее BTC"
    if alt.get("state") == "ALT_STRENGTH":
        alt_text = "альты сильнее BTC"
    elif alt.get("state") == "MIXED":
        alt_text = "альты смешанно"

    return (
        "📊 СКАН РЫНКА\n\n"
        f"🌍 Режим: {_human_market_mode(mode)}\n"
        f"Market Score: {score}/100\n"
        f"{btc_line}\n"
        f"Fear & Greed: {fng.get('value', 50)} ({fng.get('classification','Neutral')})\n"
        f"BTC trend: {trend.get('trend','UNKNOWN')}\n"
        f"Dominance: {dom_text} | {alt_text}\n"
        f"Derivatives/News: учитываются в Signal Score\n\n"
        f"💡 {_short_risk_text(market_context)}"
    )


def _decision_line(item: dict, signal: dict) -> str:
    coin = item["coin"]
    status = signal.get("status", "WATCH")
    score = int(signal.get("score", 0) or 0)
    current = float(item.get("current", 0) or 0)
    dd30 = float(item.get("drawdown_30d_high", 0) or 0)
    rel = signal.get("reliability")
    strength = (signal.get("strength_vs_btc") or {}).get("state", "?")
    price_state = (signal.get("price_attractiveness") or {}).get("state", "?")
    pos = get_position(coin)
    marker = "💼 " if pos else ""
    rel_txt = "—" if rel is None else f"{float(rel):.0f}"
    action = _decision_action_text(item, signal, pos)
    return f"{marker}{coin}: {fmt_usdt(current)} | Score {score} | price {price_state} | rel {rel_txt} | {dd30:+.1f}% | {strength} | {action}"


def format_signal_report(items: list[dict], market_context: dict | None = None) -> str:
    btc = next((x for x in items if x.get("coin") == "BTC"), None)
    if market_context is None:
        market_context = build_market_context(items)

    groups = {"STRONG_BUY": [], "ACCUMULATION": [], "WATCH": [], "AVOID": []}
    positions = []
    errors = []

    for item in items:
        if "error" in item:
            errors.append(item)
            continue
        signal = classify_signal(item, btc, market_context)
        coin = item["coin"]
        if get_position(coin):
            positions.append((item, signal))
        groups.setdefault(signal.get("status", "WATCH"), []).append((item, signal))

    for k in groups:
        groups[k].sort(key=lambda x: x[1].get("score", 0), reverse=True)
    positions.sort(key=lambda x: x[1].get("score", 0), reverse=True)

    lines = [_market_header_clean(market_context, btc), hr()]

    if positions:
        lines.append(f"💼 ТВОИ ПОЗИЦИИ ({len(positions)})")
        for item, signal in positions:
            lines.append(_signal_card(item, signal, detailed=False))
            lines.append("")
        lines.append(hr())

    # Главное изменение: показываем решение по всем монетам, а не прячем AVOID.
    lines.append("📌 РЕШЕНИЕ ПО МОНЕТАМ")
    sections = [
        ("🟢 STRONG BUY", "STRONG_BUY"),
        ("🟡 ACCUMULATION", "ACCUMULATION"),
        ("⚪ WATCH", "WATCH"),
        ("🔴 AVOID", "AVOID"),
    ]
    for title, key in sections:
        rows = groups.get(key) or []
        lines.append("")
        lines.append(f"{title} ({len(rows)})")
        if rows:
            for item, signal in rows:
                lines.append(_decision_line(item, signal))
        else:
            lines.append("нет")

    lines.append(hr())
    lines.append("🧭 Как читать:")
    lines.append("• STRONG BUY — редкий сильный сигнал")
    lines.append("• ACCUMULATION — цена уже интересна, можно рассмотреть малый вход/добор")
    lines.append("• WATCH — монета может быть сильной, но цена/фон пока не дают вход")
    lines.append("• AVOID — сейчас не лезть")
    lines.append("Покупки/продажи делай на Bybit — бот увидит их через мониторинг.")

    if errors:
        lines.append(hr())
        lines.append("⚠️ Ошибки данных:")
        for e in errors[:8]:
            lines.append(f"• {e.get('coin','?')}: {e.get('error','unknown')}")

    return "\n".join(lines)

def format_compact_signal(item: dict, signal: dict) -> str:
    """Компактная карточка сигнала для автомонитора (альты)."""
    coin = item["coin"]
    current = float(item.get("current", 0) or 0)
    dd30 = float(item.get("drawdown_30d_high", 0) or 0)
    vclass = item.get("volatility_class", "?")
    score = int(signal.get("score", 0))
    status = signal.get("status", "WATCH")
    entry_usdt = float(signal.get("entry_usdt", 0) or 0)
    pos = get_position(coin)

    status_map = {
        "STRONG_BUY": ("🟢", "STRONG BUY"),
        "ACCUMULATION": ("🟡", "ACCUMULATION"),
        "WATCH": ("⚪", "WATCH"),
        "AVOID": ("🔴", "AVOID"),
    }
    emoji, status_label = status_map.get(status, ("⚪", status))

    entry_count = int(pos.get("entry_count", 0)) if pos else 0
    tranche = entry_count + 1

    if pos and float(pos.get("next_buy_price", 0) or 0) and current:
        next_price = float(pos["next_buy_price"])
        next_pct = (next_price / current - 1) * 100
        next_text = f"${fmt_usdt(next_price)} ({next_pct:+.1f}%)"
    else:
        next_text = _next_level_text_from_vol(signal, current)

    amount_text = f"${entry_usdt:.0f} USDT" if entry_usdt else "—"
    reasons = signal.get("reasons") or []
    short_reason = reasons[0] if reasons else "нет данных"
    if len(short_reason) > 70:
        short_reason = short_reason[:67] + "…"

    out = [
        f"{emoji} {coin} — {status_label}",
        f"Цена: ${fmt_usdt(current)} | Просадка: {dd30:+.1f}% от 30д high",
        f"Транш {tranche} из 3 | Сумма: {amount_text}",
        f"Следующий уровень: {next_text}",
    ]
    vol_line = _vol_levels_line(signal)
    if vol_line:
        out.append(vol_line)
    out += [
        "",
        f"📊 Score: {score}/100 | Волатильность: {vclass}",
        f"💡 {short_reason}",
    ]
    return "\n".join(out)


def format_core_signal(item: dict, signal: dict) -> str:
    """Инфо-уведомление для BTC и ETH (без кнопок)."""
    coin = item["coin"]
    current = float(item.get("current", 0) or 0)
    dd30 = float(item.get("drawdown_30d_high", 0) or 0)
    score = int(signal.get("score", 0))
    status = signal.get("status", "WATCH")
    entry_usdt = float(signal.get("entry_usdt", 0) or 0)
    pos = get_position(coin)

    entry_count = int(pos.get("entry_count", 0)) if pos else 0
    tranche = entry_count + 1

    if pos and float(pos.get("next_buy_price", 0) or 0) and current:
        next_price = float(pos["next_buy_price"])
        next_pct = (next_price / current - 1) * 100
        next_text = f"${fmt_usdt(next_price)} ({next_pct:+.1f}%)"
    else:
        next_text = _next_level_text_from_vol(signal, current)

    amount_text = f"${entry_usdt:.0f}" if entry_usdt else "—"
    status_label = {"STRONG_BUY": "STRONG BUY"}.get(status, status)

    out = [
        f"✅ Автовход {coin} — {status_label}",
        f"Сумма: {amount_text} по ${fmt_usdt(current)}",
        f"Транш {tranche} из 3",
        f"Следующий уровень: {next_text}",
        f"Просадка: {dd30:+.1f}% | Score: {score}/100",
    ]
    vol_line = _vol_levels_line(signal)
    if vol_line:
        out.append(vol_line)
    return "\n".join(out)
