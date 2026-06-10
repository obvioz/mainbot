import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.storage import DATA_DIR, get_open_positions, load_journal

SYSTEM_LOG_PATH = DATA_DIR / "assistant_context_log.jsonl"
SYSTEM_LOG_EXPORT_PATH = DATA_DIR / "assistant_context_export.md"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _shorten(text: str, limit: int = 5000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def add_system_event(event_type: str, title: str, data: dict[str, Any] | None = None, text: str | None = None) -> None:
    row = {
        "ts": _now(),
        "type": event_type,
        "title": title,
        "data": data or {},
        "text": _shorten(text or ""),
    }
    SYSTEM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SYSTEM_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_system_events(limit: int = 100) -> list[dict[str, Any]]:
    if not SYSTEM_LOG_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in SYSTEM_LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-limit:]


def add_user_note(note: str) -> None:
    add_system_event("USER_NOTE", "Заметка пользователя", text=note)


def build_context_summary(limit: int = 30) -> str:
    events = read_system_events(limit)
    positions = get_open_positions()
    journal = load_journal()
    buys = [x for x in journal if x.get("action") == "BUY"]
    sells = [x for x in journal if x.get("action") == "SELL"]

    lines = [
        "🧠 ЖУРНАЛ ДЛЯ АНАЛИЗА",
        "",
        f"Открытых позиций: {len(positions)}",
        f"Покупок в журнале: {len(buys)}",
        f"Продаж в журнале: {len(sells)}",
        f"Событий системы: {len(events)}",
    ]
    if positions:
        lines.append("\nОткрытые позиции:")
        for coin, pos in positions.items():
            lines.append(
                f"• {coin}: qty {float(pos.get('qty',0)):.6f}, avg {float(pos.get('avg_price',0)):.6f}, "
                f"invested {float(pos.get('invested_usdt',0)):.2f} USDT, entries {pos.get('entry_count',0)}"
            )

    if events:
        lines.append("\nПоследние события:")
        for e in events[-10:]:
            lines.append(f"• {e.get('ts')} | {e.get('type')} | {e.get('title')}")
    return "\n".join(lines)


def export_context_markdown(limit: int = 200) -> Path:
    events = read_system_events(limit)
    positions = get_open_positions()
    journal = load_journal()

    lines = [
        "# Crypto Invest Bot — журнал контекста для анализа",
        "",
        f"Сформировано: {_now()}",
        "",
        "## 1. Открытые позиции",
        "",
    ]
    if positions:
        lines.append("| Монета | Qty | Средняя | Вложено USDT | Входов | Следующий добор |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for coin, pos in positions.items():
            lines.append(
                f"| {coin} | {float(pos.get('qty',0)):.8f} | {float(pos.get('avg_price',0)):.6f} | "
                f"{float(pos.get('invested_usdt',0)):.2f} | {pos.get('entry_count',0)} | {float(pos.get('next_buy_price',0)):.6f} |"
            )
    else:
        lines.append("Открытых позиций нет.")

    lines.extend(["", "## 2. Последние сделки", ""])
    if journal:
        lines.append("| Дата | Действие | Монета | Цена | Сумма USDT | PnL % | PnL USDT |")
        lines.append("|---|---|---|---:|---:|---:|---:|")
        for row in journal[-50:]:
            lines.append(
                f"| {row.get('date','')} | {row.get('action','')} | {row.get('coin','')} | "
                f"{float(row.get('price') or 0):.6f} | {float(row.get('amount_usdt') or 0):.2f} | "
                f"{float(row.get('pnl_pct') or 0):+.2f} | {float(row.get('pnl_usdt') or 0):+.2f} |"
            )
    else:
        lines.append("Сделок пока нет.")

    lines.extend(["", "## 3. События системы и рынка", ""])
    if events:
        for e in events:
            lines.append(f"### {e.get('ts')} — {e.get('type')} — {e.get('title')}")
            data = e.get("data") or {}
            if data:
                lines.append("```json")
                lines.append(json.dumps(data, ensure_ascii=False, indent=2))
                lines.append("```")
            text = e.get("text") or ""
            if text:
                lines.append(text)
            lines.append("")
    else:
        lines.append("Событий пока нет.")

    SYSTEM_LOG_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_LOG_EXPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return SYSTEM_LOG_EXPORT_PATH
