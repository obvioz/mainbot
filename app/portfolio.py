from dataclasses import dataclass
from app.config import ALLOCATION_USDT, ENTRY_STEPS, CATEGORIES
from app.formatters import fmt_money


@dataclass
class PortfolioPlanItem:
    coin: str
    category: str
    max_allocation_usdt: float
    entry_amounts_usdt: list[float]


def build_portfolio_plan() -> list[PortfolioPlanItem]:
    items = []
    for coin, allocation in ALLOCATION_USDT.items():
        items.append(
            PortfolioPlanItem(
                coin=coin,
                category=CATEGORIES.get(coin, "UNKNOWN"),
                max_allocation_usdt=allocation,
                entry_amounts_usdt=[round(allocation * step, 2) for step in ENTRY_STEPS],
            )
        )
    return items


def format_portfolio_plan() -> str:
    items = build_portfolio_plan()
    lines = ["💼 ПЛАН ПОРТФЕЛЯ — USDT\n"]
    total = 0
    for item in items:
        total += item.max_allocation_usdt
        steps = " / ".join([fmt_money(x) for x in item.entry_amounts_usdt])
        lines.append(
            f"{item.coin} [{item.category}] — лимит {fmt_money(item.max_allocation_usdt)} | входы: {steps}"
        )
    lines.append(f"\nИтого в монеты: {fmt_money(total)}")
    lines.append("Резерв держим отдельно в USDT.")
    return "\n".join(lines)
