import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.volatility import get_static_next_dca_drop

DATA_DIR = Path("data")
PORTFOLIO_PATH = DATA_DIR / "portfolio_state.json"
JOURNAL_PATH = DATA_DIR / "journal.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_portfolio() -> dict:
    return _read_json(PORTFOLIO_PATH, {"positions": {}, "alerts": {}})


def save_portfolio(state: dict) -> None:
    _write_json(PORTFOLIO_PATH, state)


def load_journal() -> list[dict]:
    return _read_json(JOURNAL_PATH, [])


def save_journal(rows: list[dict]) -> None:
    _write_json(JOURNAL_PATH, rows)


def add_journal(row: dict) -> None:
    rows = load_journal()
    rows.append({"date": now_iso(), **row})
    save_journal(rows)


def _normalize_old_position(pos: dict) -> dict:
    """Поддержка старых тестовых данных, если они были записаны в рублях."""
    if "invested_usdt" not in pos and "invested_rub" in pos:
        pos["invested_usdt"] = float(pos.get("invested_rub", 0))
    return pos


def record_buy(coin: str, amount_usdt: float, price: float, reason: str = "manual", dca_levels: list[float] | None = None) -> dict:
    coin = coin.upper().strip()
    amount_usdt = float(amount_usdt)
    price = float(price)

    if amount_usdt <= 0:
        raise ValueError("Сумма покупки должна быть больше 0 USDT")
    if price <= 0:
        raise ValueError("Цена покупки должна быть больше 0")

    state = load_portfolio()
    positions = state.setdefault("positions", {})
    pos = positions.get(coin, {
        "coin": coin,
        "qty": 0.0,
        "invested_usdt": 0.0,
        "avg_price": 0.0,
        "entry_count": 0,
        "last_entry_price": 0.0,
        "next_buy_price": 0.0,
        "take_profit_10_sent": False,
        "take_profit_15_sent": False,
        "entries": [],
    })
    pos = _normalize_old_position(pos)

    qty = amount_usdt / price

    pos["qty"] = float(pos.get("qty", 0)) + qty
    pos["invested_usdt"] = float(pos.get("invested_usdt", 0)) + amount_usdt
    pos["avg_price"] = pos["invested_usdt"] / pos["qty"] if pos["qty"] else 0
    pos["entry_count"] = int(pos.get("entry_count", 0)) + 1
    pos["last_entry_price"] = price

    # Dynamic DCA: если бот уже посчитал адаптивную лесенку по волатильности,
    # сохраняем ее в позиции и следующий добор считаем от фактической цены покупки.
    if dca_levels:
        clean_levels = [float(x) for x in dca_levels][:4]
        pos["dca_levels_used"] = clean_levels
    else:
        clean_levels = pos.get("dca_levels_used") or []

    idx = int(pos.get("entry_count", 0))
    if clean_levels and idx < len(clean_levels):
        next_drop = float(clean_levels[idx])
    else:
        next_drop = get_static_next_dca_drop(coin, idx)

    pos["next_buy_drop_pct"] = next_drop
    pos["next_buy_price"] = round(price * (1 - next_drop / 100), 8)
    pos["take_profit_10_sent"] = False
    pos["take_profit_15_sent"] = False
    pos.setdefault("entries", []).append({
        "date": now_iso(),
        "price": price,
        "amount_usdt": amount_usdt,
        "qty": qty,
        "reason": reason,
    })

    # Удаляем старое рублевое поле, если оно осталось от прошлой версии.
    pos.pop("invested_rub", None)

    positions[coin] = pos
    save_portfolio(state)

    add_journal({
        "coin": coin,
        "action": "BUY",
        "price": price,
        "amount_usdt": amount_usdt,
        "qty": qty,
        "avg_price_after": pos["avg_price"],
        "reason": reason,
    })
    return pos


def record_sell(coin: str, price: float, amount_usdt: float | None = None, sell_all: bool = False) -> dict:
    coin = coin.upper().strip()
    price = float(price)

    if price <= 0:
        raise ValueError("Цена продажи должна быть больше 0")

    state = load_portfolio()
    positions = state.setdefault("positions", {})
    if coin not in positions:
        raise ValueError(f"По {coin} нет открытой позиции")

    pos = _normalize_old_position(positions[coin])
    qty_total = float(pos.get("qty", 0))
    if qty_total <= 0:
        raise ValueError(f"По {coin} нет количества для продажи")

    current_value_usdt = qty_total * price

    if sell_all or amount_usdt is None:
        qty_sell = qty_total
        amount_usdt_sell = current_value_usdt
    else:
        amount_usdt = float(amount_usdt)
        if amount_usdt <= 0:
            raise ValueError("Сумма продажи должна быть больше 0 USDT")
        qty_sell = min(amount_usdt / price, qty_total)
        amount_usdt_sell = qty_sell * price

    share = qty_sell / qty_total
    cost_usdt = float(pos.get("invested_usdt", 0)) * share
    proceeds_usdt = qty_sell * price
    pnl_usdt = proceeds_usdt - cost_usdt
    pnl_pct = (pnl_usdt / cost_usdt * 100) if cost_usdt else 0

    remaining_qty = qty_total - qty_sell
    if remaining_qty <= 1e-12 or share > 0.999:
        positions.pop(coin, None)
    else:
        pos["qty"] = remaining_qty
        pos["invested_usdt"] = float(pos.get("invested_usdt", 0)) - cost_usdt
        pos["avg_price"] = pos["invested_usdt"] / pos["qty"] if pos["qty"] else 0
        pos.pop("invested_rub", None)
        positions[coin] = pos

    save_portfolio(state)
    result = {
        "coin": coin,
        "action": "SELL",
        "price": price,
        "amount_usdt": amount_usdt_sell,
        "qty": qty_sell,
        "pnl_usdt": pnl_usdt,
        "pnl_pct": pnl_pct,
        "sell_all": sell_all or remaining_qty <= 1e-12,
    }
    add_journal(result)
    return result


def get_position(coin: str) -> dict | None:
    pos = load_portfolio().get("positions", {}).get(coin.upper().strip())
    return _normalize_old_position(pos) if pos else None


def get_open_positions() -> dict:
    positions = load_portfolio().get("positions", {})
    return {coin: _normalize_old_position(pos) for coin, pos in positions.items()}


def export_journal_csv(path: Path | None = None) -> Path:
    """Экспорт журнала сделок в CSV для Excel/Google Sheets."""
    import csv

    rows = load_journal()
    if path is None:
        path = DATA_DIR / "journal_export.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "date", "action", "coin", "price", "amount_usdt", "qty",
        "avg_price_after", "pnl_usdt", "pnl_pct", "sell_all", "reason"
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
