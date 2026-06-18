from __future__ import annotations

import csv
import threading
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from app.storage import _atomic_write_json, _read_json, file_transaction

DATA_DIR = Path("data")
SPOT_JOURNAL_PATH = DATA_DIR / "spot_journal.json"
SPOT_JOURNAL_LOCK_PATH = DATA_DIR / "spot_journal.json.lock"

# spot_journal.json is append-only; serialize its read-modify-write across
# threads (this RLock) and processes (flock on the sidecar lock file).
_SPOT_JOURNAL_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_journal() -> list[dict]:
    # Resilient read: recovers from .bak on corruption, raises (not silent reset)
    # if both primary and backup are unreadable. Reads need only the threading
    # lock (os.replace makes writes atomic for readers).
    with _SPOT_JOURNAL_LOCK:
        return _read_json(SPOT_JOURNAL_PATH, [])


def _write_journal(rows: list[dict]) -> None:
    with file_transaction(_SPOT_JOURNAL_LOCK, SPOT_JOURNAL_LOCK_PATH):
        _atomic_write_json(SPOT_JOURNAL_PATH, rows)


def _append_journal_row(row: dict) -> None:
    """Atomic append: read-modify-write inside a cross-process file transaction."""
    with file_transaction(_SPOT_JOURNAL_LOCK, SPOT_JOURNAL_LOCK_PATH):
        rows = _read_json(SPOT_JOURNAL_PATH, [])
        rows.append(row)
        _atomic_write_json(SPOT_JOURNAL_PATH, rows)


def generate_entry_reason(indicators: dict | None) -> str:
    if not indicators:
        return "ручной вход"
    parts = []
    dd = indicators.get("drawdown_pct")
    if dd is not None:
        parts.append(f"просадка {float(dd):.1f}% от 90d max")
    score = indicators.get("score")
    if score is not None:
        s = float(score)
        label = "сильный" if s >= 60 else "умеренный" if s >= 35 else "слабый"
        parts.append(f"{label} сигнал score={s:.0f}")
    vol_ratio = indicators.get("volume_ratio")
    if vol_ratio is not None and float(vol_ratio) >= 1.5:
        parts.append(f"объём x{float(vol_ratio):.1f}")
    funding = indicators.get("funding")
    if funding is not None:
        f = float(funding)
        if f < -0.01:
            parts.append("funding отрицательный (шорты платят)")
        elif f > 0.05:
            parts.append("funding перегрет (лонги платят)")
    news = (indicators.get("news_risk") or "").upper()
    if news in {"HIGH", "EXTREME"}:
        parts.append(f"риск новостей {news}")
    vol_class = (indicators.get("volatility") or "").upper()
    if vol_class in {"HIGH", "EXTREME"}:
        parts.append(f"волатильность {vol_class}")
    return "; ".join(parts) if parts else "вход по техническому сигналу"


def log_spot_buy(
    symbol: str,
    tranche: int,
    entry_price: float,
    amount_usdt: float,
    qty: float,
    trigger: str = "",
    trigger_pct: float | None = None,
    auto: bool = False,
    indicators: dict | None = None,
    portfolio_usdt_before: float | None = None,
    reason: str = "",
) -> dict:
    row: dict[str, Any] = {
        "timestamp": _now_iso(),
        "symbol": symbol.upper(),
        "side": "BUY",
        "tranche": tranche,
        "entry_price": round(float(entry_price), 8),
        "amount_usdt": round(float(amount_usdt), 4),
        "qty": round(float(qty), 8),
        "trigger": trigger,
        "trigger_pct": trigger_pct,
        "auto": auto,
        "indicators": indicators or {},
        "reason": reason or generate_entry_reason(indicators),
        "portfolio_usdt_before": portfolio_usdt_before,
    }
    _append_journal_row(row)
    return row


def log_spot_sell(
    symbol: str,
    exit_type: str,
    entry_price_avg: float,
    exit_price: float,
    qty_sold: float,
    pnl_pct: float,
    pnl_usdt: float,
    hold_hours: float = 0.0,
    trailing_max_price: float | None = None,
    exit_reason: str = "",
) -> dict:
    row: dict[str, Any] = {
        "timestamp": _now_iso(),
        "symbol": symbol.upper(),
        "side": "SELL",
        "exit_type": exit_type,
        "entry_price_avg": round(float(entry_price_avg), 8),
        "exit_price": round(float(exit_price), 8),
        "qty_sold": round(float(qty_sold), 8),
        "pnl_pct": round(float(pnl_pct), 4),
        "pnl_usdt": round(float(pnl_usdt), 4),
        "hold_hours": round(float(hold_hours), 2),
        "trailing_max_price": trailing_max_price,
        "exit_reason": exit_reason,
    }
    _append_journal_row(row)
    return row


def format_spot_journal(limit: int = 10) -> str:
    rows = _read_journal()
    if not rows:
        return "📒 SPOT JOURNAL\n\nЗаписей пока нет."
    recent = rows[-limit:][::-1]
    lines = ["📒 SPOT JOURNAL — последние записи", ""]
    for r in recent:
        ts = (r.get("timestamp") or "")[:16].replace("T", " ")
        sym = r.get("symbol", "?")
        side = r.get("side", "?")
        if side == "BUY":
            price = r.get("entry_price", 0)
            usdt = r.get("amount_usdt", 0)
            tranche = r.get("tranche", "?")
            trigger = r.get("trigger") or "—"
            auto_flag = "🤖 авто" if r.get("auto") else "👤 вручную"
            lines.append(f"🟢 BUY {sym} — {ts} ({auto_flag})")
            lines.append(f"  Транш #{tranche} | ${float(price):.4f} | {float(usdt):.2f} USDT")
            lines.append(f"  Триггер: {trigger}")
            reason = r.get("reason", "")
            if reason:
                lines.append(f"  {reason[:80]}")
        else:
            exit_type = r.get("exit_type", "?")
            exit_price = r.get("exit_price", 0)
            pnl_pct = r.get("pnl_pct", 0)
            pnl_usdt = r.get("pnl_usdt", 0)
            hold_h = r.get("hold_hours", 0)
            icon = "🔴" if exit_type == "trailing" else "🟡" if exit_type == "TP1" else "⚪"
            lines.append(f"{icon} SELL {sym} — {ts} ({exit_type})")
            lines.append(f"  Цена: ${float(exit_price):.4f} | PnL {float(pnl_pct):+.2f}% / {float(pnl_usdt):+.2f} USDT")
            lines.append(f"  Удержано: {float(hold_h):.1f}ч")
        lines.append("")
    return "\n".join(lines).rstrip()


def _period_rows(days: int) -> list[dict]:
    rows = _read_journal()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r for r in rows if (r.get("timestamp") or "") >= cutoff]


def format_spot_report(days: int = 7) -> str:
    period = _period_rows(days)
    if not period:
        return f"📊 SPOT REPORT ({days}д)\n\nНет данных за этот период."

    buys = [r for r in period if r.get("side") == "BUY"]
    sells = [r for r in period if r.get("side") == "SELL"]

    tp1_sells = [r for r in sells if r.get("exit_type") == "TP1"]
    trailing_sells = [r for r in sells if r.get("exit_type") == "trailing"]
    manual_sells = [r for r in sells if r.get("exit_type") == "manual"]

    pnl_values = [float(r["pnl_pct"]) for r in sells if r.get("pnl_pct") is not None]
    avg_pnl = sum(pnl_values) / len(pnl_values) if pnl_values else 0.0
    total_pnl_usdt = sum(float(r.get("pnl_usdt", 0)) for r in sells)
    win_sells = [r for r in sells if float(r.get("pnl_pct", 0)) > 0]
    wr = len(win_sells) / len(sells) * 100 if sells else 0.0

    scores = [float(r["indicators"]["score"]) for r in buys if r.get("indicators", {}).get("score") is not None]
    avg_score = sum(scores) / len(scores) if scores else 0.0

    coin_counts = Counter(r.get("symbol", "?") for r in buys)
    top_coins = coin_counts.most_common(5)

    lines = [
        f"📊 SPOT REPORT — {days} дней",
        "",
        f"Входов: {len(buys)}",
    ]
    if scores:
        lines.append(f"Средний score: {avg_score:.1f}")
    lines += [
        f"Выходов: {len(sells)}  (TP1: {len(tp1_sells)} | трейлинг: {len(trailing_sells)} | ручные: {len(manual_sells)})",
    ]
    if pnl_values:
        lines += [
            f"Средний PnL: {avg_pnl:+.2f}%",
            f"Итого PnL: {total_pnl_usdt:+.2f} USDT",
            f"Винрейт: {wr:.1f}%",
        ]
    else:
        lines.append("PnL: нет закрытых сделок")
    if top_coins:
        lines += ["", "Монеты (по частоте входов):"]
        for coin, cnt in top_coins:
            lines.append(f"  {coin}: {cnt}x")
    return "\n".join(lines)


def export_spot_csv() -> Path:
    rows = _read_journal()
    path = DATA_DIR / "spot_journal_export.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    all_fields = [
        "timestamp", "symbol", "side",
        "tranche", "entry_price", "amount_usdt", "qty",
        "trigger", "trigger_pct", "auto", "reason", "portfolio_usdt_before",
        "ind_drawdown_pct", "ind_score", "ind_volume_ratio",
        "ind_funding", "ind_news_risk", "ind_volatility",
        "exit_type", "entry_price_avg", "exit_price", "qty_sold",
        "pnl_pct", "pnl_usdt", "hold_hours", "trailing_max_price", "exit_reason",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        for row in rows:
            flat = {k: v for k, v in row.items() if k != "indicators"}
            ind = row.get("indicators") or {}
            flat["ind_drawdown_pct"] = ind.get("drawdown_pct")
            flat["ind_score"] = ind.get("score")
            flat["ind_volume_ratio"] = ind.get("volume_ratio")
            flat["ind_funding"] = ind.get("funding")
            flat["ind_news_risk"] = ind.get("news_risk")
            flat["ind_volatility"] = ind.get("volatility")
            writer.writerow(flat)
    return path
