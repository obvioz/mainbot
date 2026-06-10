from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import ccxt
import pandas as pd

from app.config import COINS, CATEGORIES, BASE_DCA_LEVELS, TAKE_PROFIT_LEVELS, normalize_coin
from app.strategy_params import get_dca_levels, get_tp_levels
from app.settings import settings


DEFAULT_START = "2026-04-01"
DEFAULT_END = "2026-05-31"
DEFAULT_ENTRY_USDT = 10.0
MAX_EVENTS_PER_COIN = 12


@dataclass
class BacktestEvent:
    date: str
    coin: str
    action: str
    price: float
    amount_usdt: float = 0.0
    qty: float = 0.0
    avg_price: float = 0.0
    pnl_pct: float | None = None
    reason: str = ""


@dataclass
class BacktestPosition:
    coin: str
    qty: float = 0.0
    invested_usdt: float = 0.0
    avg_price: float = 0.0
    last_buy_price: float = 0.0
    entry_count: int = 0
    realized_pnl_usdt: float = 0.0
    cycles_closed: int = 0
    events: list[BacktestEvent] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.qty > 0 and self.entry_count > 0

    def buy(self, date: str, price: float, amount_usdt: float, reason: str) -> None:
        qty = amount_usdt / price
        self.qty += qty
        self.invested_usdt += amount_usdt
        self.avg_price = self.invested_usdt / self.qty if self.qty else 0.0
        self.last_buy_price = price
        self.entry_count += 1
        self.events.append(
            BacktestEvent(
                date=date,
                coin=self.coin,
                action="BUY",
                price=price,
                amount_usdt=amount_usdt,
                qty=qty,
                avg_price=self.avg_price,
                reason=reason,
            )
        )

    def sell_all(self, date: str, price: float, reason: str) -> None:
        proceeds = self.qty * price
        pnl_usdt = proceeds - self.invested_usdt
        pnl_pct = (price / self.avg_price - 1) * 100 if self.avg_price else 0.0
        self.realized_pnl_usdt += pnl_usdt
        self.cycles_closed += 1
        self.events.append(
            BacktestEvent(
                date=date,
                coin=self.coin,
                action="SELL_ALL",
                price=price,
                amount_usdt=proceeds,
                qty=self.qty,
                avg_price=self.avg_price,
                pnl_pct=pnl_pct,
                reason=reason,
            )
        )
        self.qty = 0.0
        self.invested_usdt = 0.0
        self.avg_price = 0.0
        self.last_buy_price = 0.0
        self.entry_count = 0


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def make_public_exchange():
    exchange_cls = getattr(ccxt, settings.exchange_id)
    return exchange_cls({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
            "recvWindow": settings.bybit_recv_window,
        },
    })


def _symbol(coin: str) -> str:
    coin = normalize_coin(coin)
    return f"{coin}/{settings.quote}"


def fetch_daily_history(exchange, coin: str, start: str, end: str, warmup_days: int = 120) -> pd.DataFrame:
    """Fetch daily OHLCV with warmup days before start, so rolling highs work."""
    start_dt = _parse_date(start) - timedelta(days=warmup_days)
    end_dt = _parse_date(end) + timedelta(days=2)
    since = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    symbol = _symbol(coin)
    rows: list[list[Any]] = []

    while since < end_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe="1d", since=since, limit=200)
        if not batch:
            break
        rows.extend(batch)
        last_ts = batch[-1][0]
        next_since = last_ts + 24 * 60 * 60 * 1000
        if next_since <= since:
            break
        since = next_since
        if len(batch) < 200:
            break

    if not rows:
        raise ValueError(f"Нет свечей по {symbol}")

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)


def _category(coin: str) -> str:
    return CATEGORIES.get(coin, "STRONG_ALT")


def _dca_levels(coin: str) -> list[float]:
    return list(get_dca_levels(coin))


def _tp_first(coin: str) -> float:
    return float(get_tp_levels(coin)[0])


def backtest_coin(
    coin: str,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    entry_usdt: float = DEFAULT_ENTRY_USDT,
) -> dict[str, Any]:
    exchange = make_public_exchange()
    df = fetch_daily_history(exchange, coin, start, end)
    levels = _dca_levels(coin)
    tp_pct = _tp_first(coin)
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    pos = BacktestPosition(coin=coin)
    first_signal_count = 0
    max_entries_used = 0

    # Rolling high is shifted by one day, so today's signal does not peek into today's high.
    df["rolling_high_30"] = df["high"].rolling(30).max().shift(1)

    for _, row in df.iterrows():
        dt = datetime.fromtimestamp(row["timestamp"] / 1000, tz=timezone.utc)
        if dt < start_dt or dt > end_dt:
            continue

        date = row["date"]
        close = float(row["close"])
        high30 = float(row["rolling_high_30"]) if pd.notna(row["rolling_high_30"]) else 0.0
        if not high30:
            continue

        if not pos.is_open:
            drawdown = (close / high30 - 1) * 100
            if drawdown <= -levels[0]:
                first_signal_count += 1
                pos.buy(date, close, entry_usdt, f"1-й вход: просадка от 30d high {drawdown:.1f}%")
                max_entries_used = max(max_entries_used, pos.entry_count)
            continue

        # If take-profit is hit by daily close, close the cycle.
        pnl_pct = (close / pos.avg_price - 1) * 100 if pos.avg_price else 0.0
        if pnl_pct >= tp_pct:
            pos.sell_all(date, close, f"TP {tp_pct:.0f}% от средней")
            continue

        # DCA from actual last buy price. If entry_count=1, next level is levels[1].
        if pos.entry_count < len(levels):
            next_drop = levels[pos.entry_count]
            next_buy_price = pos.last_buy_price * (1 - next_drop / 100)
            if close <= next_buy_price:
                pos.buy(date, close, entry_usdt, f"Добор #{pos.entry_count + 1}: -{next_drop:.0f}% от последней покупки")
                max_entries_used = max(max_entries_used, pos.entry_count)

    last_close = float(df.iloc[-1]["close"])
    if pos.is_open:
        # last candle within end range, not necessarily last fetched warmup/end+2 candle.
        in_range = df[(df["date"] >= start) & (df["date"] <= end)]
        if not in_range.empty:
            last_close = float(in_range.iloc[-1]["close"])
        unrealized_pct = (last_close / pos.avg_price - 1) * 100 if pos.avg_price else 0.0
        unrealized_usdt = pos.qty * last_close - pos.invested_usdt
    else:
        unrealized_pct = 0.0
        unrealized_usdt = 0.0

    closed_events = [e for e in pos.events if e.action == "SELL_ALL"]
    wins = [e for e in closed_events if (e.pnl_pct or 0) > 0]
    losses = [e for e in closed_events if (e.pnl_pct or 0) <= 0]
    total_buys = len([e for e in pos.events if e.action == "BUY"])

    return {
        "coin": coin,
        "category": _category(coin),
        "period": f"{start} — {end}",
        "entry_usdt": entry_usdt,
        "dca_levels": levels,
        "tp_pct": tp_pct,
        "signals": first_signal_count,
        "buys": total_buys,
        "closed_cycles": len(closed_events),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": (len(wins) / len(closed_events) * 100) if closed_events else None,
        "realized_pnl_usdt": pos.realized_pnl_usdt,
        "open": pos.is_open,
        "open_avg_price": pos.avg_price,
        "open_invested_usdt": pos.invested_usdt,
        "last_price": last_close,
        "unrealized_pct": unrealized_pct,
        "unrealized_usdt": unrealized_usdt,
        "max_entries_used": max_entries_used,
        "events": [e.__dict__ for e in pos.events],
    }


def backtest_all(start: str = DEFAULT_START, end: str = DEFAULT_END, entry_usdt: float = DEFAULT_ENTRY_USDT) -> list[dict[str, Any]]:
    results = []
    for coin in COINS:
        try:
            results.append(backtest_coin(coin, start, end, entry_usdt))
        except Exception as exc:
            results.append({"coin": coin, "error": str(exc), "period": f"{start} — {end}"})
    return results


def _fmt_money(value: float) -> str:
    return f"{value:+.2f} USDT" if value < 0 else f"+{value:.2f} USDT"


def format_backtest_coin_report(result: dict[str, Any]) -> str:
    if "error" in result:
        return f"🧪 BACKTEST {result.get('coin')}\n\nОшибка: {result['error']}"

    coin = result["coin"]
    lines = [
        f"🧪 BACKTEST — {coin}",
        f"Период: {result['period']}",
        f"Категория: {result['category']}",
        f"Лесенка: {' / '.join(str(int(x))+'%' for x in result['dca_levels'])}",
        f"TP теста: +{result['tp_pct']:.0f}% от средней",
        "",
        "📊 Итог:",
        f"• первых сигналов: {result['signals']}",
        f"• покупок всего: {result['buys']}",
        f"• закрытых циклов: {result['closed_cycles']}",
    ]
    if result["closed_cycles"]:
        lines.append(f"• winrate: {result['winrate']:.1f}%")
        lines.append(f"• realized PnL: {_fmt_money(result['realized_pnl_usdt'])}")
    else:
        lines.append("• закрытых сделок не было")

    if result["open"]:
        lines += [
            "",
            "📌 На конец периода позиция осталась открытой:",
            f"• средняя: {result['open_avg_price']:.4f}",
            f"• последняя цена: {result['last_price']:.4f}",
            f"• floating PnL: {result['unrealized_pct']:+.2f}% / {_fmt_money(result['unrealized_usdt'])}",
        ]

    events = result.get("events") or []
    if events:
        lines += ["", "🧾 События:"]
        for e in events[:MAX_EVENTS_PER_COIN]:
            if e["action"] == "BUY":
                lines.append(f"• {e['date']} BUY {e['amount_usdt']:.0f} USDT по {e['price']:.4f} | средняя {e['avg_price']:.4f} — {e['reason']}")
            else:
                lines.append(f"• {e['date']} SELL ALL по {e['price']:.4f} | {e.get('pnl_pct', 0):+.2f}% — {e['reason']}")
        if len(events) > MAX_EVENTS_PER_COIN:
            lines.append(f"• … еще {len(events)-MAX_EVENTS_PER_COIN} событий")
    else:
        lines += ["", "Сигналов по этой логике за период не было."]

    return "\n".join(lines)


def format_backtest_all_report(results: list[dict[str, Any]]) -> str:
    ok = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    closed_cycles = sum(r["closed_cycles"] for r in ok)
    wins = sum(r["wins"] for r in ok)
    realized = sum(r["realized_pnl_usdt"] for r in ok)
    open_count = sum(1 for r in ok if r["open"])
    floating = sum(r["unrealized_usdt"] for r in ok)
    winrate = (wins / closed_cycles * 100) if closed_cycles else 0.0
    period = ok[0]["period"] if ok else (results[0].get("period") if results else "?")

    lines = [
        "🧪 BACKTEST ALL",
        f"Период: {period}",
        "Модель: 10 USDT на каждый вход, sell all на первом TP",
        "",
        "📊 Общий итог:",
        f"• закрытых циклов: {closed_cycles}",
        f"• winrate: {winrate:.1f}%" if closed_cycles else "• winrate: нет закрытых циклов",
        f"• realized PnL: {_fmt_money(realized)}",
        f"• открытых позиций на конец: {open_count}",
        f"• floating PnL: {_fmt_money(floating)}",
        "",
        "📌 По монетам:",
    ]
    for r in ok:
        status = "OPEN" if r["open"] else "CLOSED/NO POS"
        win = f"WR {r['winrate']:.0f}%" if r["winrate"] is not None else "WR —"
        lines.append(
            f"• {r['coin']}: buys {r['buys']} | cycles {r['closed_cycles']} | {win} | "
            f"realized {_fmt_money(r['realized_pnl_usdt'])} | floating {r['unrealized_pct']:+.1f}% | {status}"
        )
    if errors:
        lines += ["", "⚠️ Ошибки данных:"]
        for r in errors[:8]:
            lines.append(f"• {r['coin']}: {r['error']}")
    lines += [
        "",
        "Важно: это грубый дневной бэктест по close-свечам. Он не учитывает комиссии, спред, intraday-проколы и новости. Его задача — понять, была ли логика стратегии жизнеспособной на истории.",
    ]
    return "\n".join(lines)
