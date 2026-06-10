from __future__ import annotations

from app.storage import get_open_positions, load_journal
from app.config import ALLOCATION_USDT


def fmt_usdt(x: float) -> str:
    x = float(x or 0)
    if abs(x) >= 1000:
        return f"{x:,.0f}".replace(",", " ")
    if abs(x) >= 10:
        return f"{x:.2f}"
    return f"{x:.4f}"


def fmt_money(x: float) -> str:
    return f"{fmt_usdt(x)} USDT"


def format_position_line(pos: dict, current_price: float | None = None) -> str:
    coin = pos["coin"]
    qty = float(pos.get("qty", 0))
    avg = float(pos.get("avg_price", 0))
    invested = float(pos.get("invested_usdt", pos.get("invested_rub", 0)))
    entry_count = int(pos.get("entry_count", 0))
    next_buy = float(pos.get("next_buy_price", 0))

    if current_price:
        value = qty * current_price
        pnl_pct = (current_price / avg - 1) * 100 if avg else 0
        pnl = value - invested
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        return (
            f"{emoji} {coin}\n"
            f"  Кол-во: {qty:.6f}\n"
            f"  Вложено: {fmt_money(invested)}\n"
            f"  Средняя: {fmt_usdt(avg)} USDT\n"
            f"  Сейчас: {fmt_usdt(current_price)} USDT\n"
            f"  Стоимость: {fmt_money(value)}\n"
            f"  PnL: {pnl_pct:+.2f}% / {fmt_money(pnl)}\n"
            f"  Входов: {entry_count} | след. добор: {fmt_usdt(next_buy)} USDT"
        )

    return (
        f"⚪ {coin}\n"
        f"  Кол-во: {qty:.6f}\n"
        f"  Средняя: {fmt_usdt(avg)} USDT\n"
        f"  Вложено: {fmt_money(invested)}\n"
        f"  Входов: {entry_count} | след. добор: {fmt_usdt(next_buy)} USDT"
    )


def format_positions_report(price_map: dict[str, float] | None = None) -> str:
    positions = get_open_positions()
    if not positions:
        return (
            "💼 Открытых позиций пока нет.\n\n"
            "После покупки введи, например:\n"
            "/buy ETH 10 2178.5\n\n"
            "Где 10 — сумма в USDT, 2178.5 — твоя фактическая цена покупки."
        )

    lines = ["💼 ОТКРЫТЫЕ ПОЗИЦИИ\n"]
    total_invested = 0.0
    total_value = 0.0
    for coin, pos in positions.items():
        current = price_map.get(coin) if price_map else None
        lines.append(format_position_line(pos, current))
        lines.append("")
        total_invested += float(pos.get("invested_usdt", pos.get("invested_rub", 0)))
        if current:
            total_value += float(pos.get("qty", 0)) * current

    lines.append("━━━━━━━━━━━━")
    lines.append(f"Вложено: {fmt_money(total_invested)}")
    if total_value:
        pnl = total_value - total_invested
        pnl_pct = pnl / total_invested * 100 if total_invested else 0
        lines.append(f"Текущая стоимость: {fmt_money(total_value)}")
        lines.append(f"Общий PnL: {pnl_pct:+.2f}% / {fmt_money(pnl)}")
    return "\n".join(lines)


def format_journal(limit: int = 10) -> str:
    rows = load_journal()
    if not rows:
        return "📒 Журнал пока пуст."
    lines = [f"📒 ЖУРНАЛ СДЕЛОК — последние {min(limit, len(rows))}\n"]
    for r in rows[-limit:][::-1]:
        action = r.get("action")
        emoji = "🟢" if action == "BUY" else "🔵"
        coin = r.get("coin")
        price = r.get("price", 0)
        amount = r.get("amount_usdt", r.get("amount_rub", 0))
        date = r.get("date", "")[:16].replace("T", " ")
        qty = float(r.get("qty", 0))
        extra = ""
        if action == "SELL":
            extra = f" | PnL {r.get('pnl_pct', 0):+.2f}% / {fmt_money(r.get('pnl_usdt', r.get('pnl_rub', 0)))}"
        lines.append(
            f"{emoji} {date} — {action} {coin} на {fmt_money(amount)} "
            f"по {fmt_usdt(price)} | qty {qty:.6f}{extra}"
        )
    return "\n".join(lines)


def format_stats_report(price_map: dict[str, float] | None = None) -> str:
    rows = load_journal()
    positions = get_open_positions()
    buys = [r for r in rows if r.get("action") == "BUY"]
    sells = [r for r in rows if r.get("action") == "SELL"]

    total_buys = sum(float(r.get("amount_usdt", 0) or 0) for r in buys)
    realized_pnl = sum(float(r.get("pnl_usdt", 0) or 0) for r in sells)
    wins = [r for r in sells if float(r.get("pnl_usdt", 0) or 0) > 0]
    losses = [r for r in sells if float(r.get("pnl_usdt", 0) or 0) < 0]
    flat = [r for r in sells if abs(float(r.get("pnl_usdt", 0) or 0)) < 1e-12]
    win_rate = len(wins) / len(sells) * 100 if sells else 0.0
    avg_pnl_pct = sum(float(r.get("pnl_pct", 0) or 0) for r in sells) / len(sells) if sells else 0.0
    best = max(sells, key=lambda r: float(r.get("pnl_pct", 0) or 0), default=None)
    worst = min(sells, key=lambda r: float(r.get("pnl_pct", 0) or 0), default=None)

    open_invested = sum(float(p.get("invested_usdt", 0) or 0) for p in positions.values())
    open_value = 0.0
    if price_map:
        for coin, p in positions.items():
            current = price_map.get(coin)
            if current:
                open_value += float(p.get("qty", 0) or 0) * current
    unrealized_pnl = open_value - open_invested if open_value else 0.0
    unrealized_pct = unrealized_pnl / open_invested * 100 if open_value and open_invested else 0.0

    lines = ["📊 СТАТИСТИКА СТРАТЕГИИ\n"]
    lines.append(f"Покупок: {len(buys)} | Продаж: {len(sells)}")
    lines.append(f"Закрытых/частично закрытых сделок: {len(sells)}")
    lines.append(f"Успешных: {len(wins)} | Убыточных: {len(losses)} | В ноль: {len(flat)}")
    lines.append(f"Win rate: {win_rate:.1f}%")
    lines.append("")
    lines.append(f"Реализованный PnL: {fmt_money(realized_pnl)}")
    lines.append(f"Средний результат продажи: {avg_pnl_pct:+.2f}%")
    if best:
        lines.append(f"Лучшая: {best.get('coin')} {float(best.get('pnl_pct', 0)):+.2f}% / {fmt_money(best.get('pnl_usdt', 0))}")
    if worst:
        lines.append(f"Худшая: {worst.get('coin')} {float(worst.get('pnl_pct', 0)):+.2f}% / {fmt_money(worst.get('pnl_usdt', 0))}")
    lines.append("")
    lines.append(f"Всего куплено за историю: {fmt_money(total_buys)}")
    lines.append(f"Открытых позиций: {len(positions)}")
    lines.append(f"В открытых позициях: {fmt_money(open_invested)}")
    if open_value:
        lines.append(f"Текущая стоимость открытых: {fmt_money(open_value)}")
        lines.append(f"Нереализованный PnL: {unrealized_pct:+.2f}% / {fmt_money(unrealized_pnl)}")
    lines.append(f"Итого PnL: {fmt_money(realized_pnl + unrealized_pnl)}")
    return "\n".join(lines)


def allocation_left_text(coin: str, invested_usdt: float) -> str:
    limit = ALLOCATION_USDT.get(coin, 0)
    if not limit:
        return ""
    left = max(limit - invested_usdt, 0)
    return f"Лимит: {fmt_money(limit)} | осталось: {fmt_money(left)}"


def format_risk_report(price_map: dict[str, float] | None = None) -> str:
    from app.risk_manager import get_portfolio_risk

    r = get_portfolio_risk(price_map)
    lines = ["🛡️ РИСКИ ПОРТФЕЛЯ\n"]
    lines.append("Капитал не фиксируем: бот считает только фактически внесенные сделки.")
    lines.append(f"В открытых позициях: {fmt_money(r['total_invested'])}")
    lines.append(f"Текущая оценка позиций: {fmt_money(r['total_value'])}")
    lines.append(f"Нереализованный PnL: {fmt_money(r['unrealized_pnl'])}")
    lines.append(f"Реализованный PnL: {fmt_money(r['realized_pnl'])}")
    lines.append(f"Уровень риска: {'🟡 ELEVATED' if r['risk_level']=='ELEVATED' else '🟢 NORMAL'}")

    if r['warnings']:
        lines.append("")
        lines.append("⚠️ Предупреждения:")
        for w in r['warnings']:
            lines.append(f"• {w}")

    lines.append("")
    lines.append("📦 По категориям:")
    if not r['by_category']:
        lines.append("Открытых позиций пока нет.")
    else:
        total = r['total_invested'] or 1
        for cat, d in r['by_category'].items():
            pct = d['invested'] / total * 100
            lines.append(f"• {cat}: {fmt_money(d['invested'])} ({pct:.1f}% от вложенного), позиций: {int(d['count'])}")

    lines.append("")
    lines.append("🪙 По открытым монетам:")
    if not r['coins']:
        lines.append("Открытых позиций пока нет.")
    else:
        for c in r['coins'][:12]:
            status = "🟡" if c['soft_limit'] and c['invested'] > c['soft_limit'] else "🟢"
            lines.append(
                f"{status} {c['coin']}: {fmt_money(c['invested'])} | "
                f"входов {c['entry_count']}/{c['max_entries']} | "
                f"мягкий ориентир {fmt_money(c['soft_limit'])}"
            )

    lines.append("")
    lines.append("Жесткий запрет оставлен только на количество входов. Лимиты — подсказки, не блокировка.")
    return "\n".join(lines)


def format_buy_risk_warning(check: dict) -> str:
    parts = []
    if check.get('blockers'):
        parts.append("🚫 Покупка нарушает жесткое правило:")
        parts.extend([f"• {x}" for x in check['blockers']])
    if check.get('warnings'):
        parts.append("⚠️ Предупреждения:")
        parts.extend([f"• {x}" for x in check['warnings']])
    if not parts:
        parts.append("🟢 Риск по позиции нормальный.")
    parts.append(f"После покупки в монете будет: {fmt_money(check['invested_after'])}")
    return "\n".join(parts)
