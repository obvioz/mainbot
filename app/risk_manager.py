from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import ALLOCATION_USDT, CATEGORIES, ENTRY_STEPS
from app.storage import get_open_positions, load_journal

# Модельный капитал. Его можно менять под себя в config.py.
# Если переменной нет — считаем по сумме лимитов, но это хуже для контроля риска.
def position_value(pos: dict, current_price: float | None = None) -> float:
    if current_price:
        return float(pos.get("qty", 0) or 0) * float(current_price)
    return float(pos.get("invested_usdt", 0) or 0)


def get_realized_pnl() -> float:
    rows = load_journal()
    return sum(float(r.get("pnl_usdt", 0) or 0) for r in rows if r.get("action") == "SELL")


def coin_soft_limit(coin: str) -> float:
    # Мягкий ориентир для размера позиции, НЕ жесткий запрет.
    return float(ALLOCATION_USDT.get(coin.upper(), 0) or 0)


def get_coin_risk(coin: str, price_map: dict[str, float] | None = None) -> dict[str, Any]:
    coin = coin.upper()
    positions = get_open_positions()
    pos = positions.get(coin)
    soft_limit = coin_soft_limit(coin)
    invested = float(pos.get("invested_usdt", 0) or 0) if pos else 0.0
    current_price = price_map.get(coin) if price_map else None
    value = position_value(pos, current_price) if pos else 0.0
    used_pct = invested / soft_limit * 100 if soft_limit else 0.0
    left = max(soft_limit - invested, 0.0) if soft_limit else 0.0
    entry_count = int(pos.get("entry_count", 0) or 0) if pos else 0
    max_entries = len(ENTRY_STEPS)
    return {
        "coin": coin,
        "category": CATEGORIES.get(coin, "UNKNOWN"),
        "soft_limit": soft_limit,
        "limit": soft_limit,  # совместимость со старым кодом
        "invested": invested,
        "value": value,
        "left": left,
        "used_pct": used_pct,
        "entry_count": entry_count,
        "max_entries": max_entries,
        "position": pos,
    }


def get_portfolio_risk(price_map: dict[str, float] | None = None) -> dict[str, Any]:
    positions = get_open_positions()
    realized = get_realized_pnl()
    total_invested = 0.0
    total_value = 0.0
    by_category: dict[str, dict[str, float]] = {}
    coins: list[dict[str, Any]] = []

    for coin, pos in positions.items():
        invested = float(pos.get("invested_usdt", 0) or 0)
        current = price_map.get(coin) if price_map else None
        value = position_value(pos, current)
        cat = CATEGORIES.get(coin, "UNKNOWN")
        total_invested += invested
        total_value += value if current else invested
        by_category.setdefault(cat, {"invested": 0.0, "value": 0.0, "count": 0})
        by_category[cat]["invested"] += invested
        by_category[cat]["value"] += value if current else invested
        by_category[cat]["count"] += 1
        coins.append(get_coin_risk(coin, price_map))

    unrealized = total_value - total_invested
    risk_level = "NORMAL"
    warnings: list[str] = []

    # Предупреждаем только о концентрации, ничего не блокируем по общему капиталу.
    if coins:
        largest = max(coins, key=lambda x: x["invested"])
        if total_invested > 0 and largest["invested"] / total_invested >= 0.35:
            risk_level = "ELEVATED"
            warnings.append(f"высокая концентрация в {largest['coin']} — {largest['invested']/total_invested*100:.0f}% вложений")
        over_soft = [c for c in coins if c["soft_limit"] and c["invested"] > c["soft_limit"]]
        if over_soft:
            risk_level = "ELEVATED"
            warnings.append("часть монет выше мягкого ориентира позиции")

    return {
        "realized_pnl": realized,
        "total_invested": total_invested,
        "total_value": total_value,
        "unrealized_pnl": unrealized,
        "by_category": by_category,
        "coins": sorted(coins, key=lambda x: x["invested"], reverse=True),
        "risk_level": risk_level,
        "warnings": warnings,
    }


def check_buy_risk(coin: str, amount_usdt: float, price: float | None = None) -> dict[str, Any]:
    coin = coin.upper()
    amount_usdt = float(amount_usdt)
    risk = get_portfolio_risk()
    coin_risk = get_coin_risk(coin)
    soft_limit = coin_risk["soft_limit"]
    invested_after = coin_risk["invested"] + amount_usdt

    blockers: list[str] = []
    warnings: list[str] = []

    # Единственный жесткий стоп — максимум входов по монете.
    if coin_risk["entry_count"] >= coin_risk["max_entries"]:
        blockers.append(f"по {coin} уже использованы все {coin_risk['max_entries']} входа")

    # Мягкие предупреждения, без запрета покупки.
    if soft_limit and invested_after > soft_limit:
        warnings.append(
            f"{coin} будет выше мягкого ориентира: {invested_after:.2f}/{soft_limit:.2f} USDT"
        )

    total_after = risk["total_invested"] + amount_usdt
    if total_after > 0 and invested_after / total_after >= 0.35:
        warnings.append(f"высокая концентрация в {coin}: около {invested_after/total_after*100:.0f}% вложений")

    return {
        "allowed": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "coin_risk": coin_risk,
        "portfolio_risk": risk,
        "invested_after": invested_after,
    }
