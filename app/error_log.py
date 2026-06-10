from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from app.storage import DATA_DIR

ERROR_LOG_PATH = DATA_DIR / "error_log.jsonl"
ERROR_LOG_EXPORT_PATH = DATA_DIR / "error_log_export.md"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _short(value: str, limit: int = 8000) -> str:
    value = value or ""
    return value if len(value) <= limit else value[:limit] + "\n...[truncated]"


def log_error(source: str, exc: BaseException, context: dict[str, Any] | None = None) -> None:
    """Append an error event to data/error_log.jsonl.

    The goal is to let the user export errors to ChatGPT for debugging without
    copying terminal tracebacks by hand.
    """
    row = {
        "ts": _now(),
        "source": source,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "context": context or {},
        "traceback": _short("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
    }
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_errors(limit: int = 50) -> list[dict[str, Any]]:
    if not ERROR_LOG_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in ERROR_LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-limit:]


def format_error_summary(limit: int = 10) -> str:
    rows = read_errors(limit)
    if not rows:
        return "✅ Журнал ошибок пуст."

    lines = ["⚠️ ЖУРНАЛ ОШИБОК", "", f"Последних ошибок: {len(rows)}"]
    for row in rows:
        msg = row.get("message", "")
        if len(msg) > 180:
            msg = msg[:177] + "…"
        lines.append(
            f"• {row.get('ts')} | {row.get('source')} | {row.get('error_type')}: {msg}"
        )
    lines += ["", "Для выгрузки полного файла: /exporterrors"]
    return "\n".join(lines)


def export_errors_markdown(limit: int = 200) -> Path:
    rows = read_errors(limit)
    lines = [
        "# Crypto Invest Bot — журнал ошибок",
        "",
        f"Сформировано: {_now()}",
        "",
    ]
    if not rows:
        lines.append("Ошибок пока нет.")
    else:
        for row in rows:
            lines.append(f"## {row.get('ts')} — {row.get('source')} — {row.get('error_type')}")
            lines.append("")
            lines.append(f"**Message:** {row.get('message','')}")
            context = row.get("context") or {}
            if context:
                lines.append("")
                lines.append("**Context:**")
                lines.append("```json")
                lines.append(json.dumps(context, ensure_ascii=False, indent=2))
                lines.append("```")
            tb = row.get("traceback") or ""
            if tb:
                lines.append("")
                lines.append("**Traceback:**")
                lines.append("```text")
                lines.append(tb)
                lines.append("```")
            lines.append("")

    ERROR_LOG_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ERROR_LOG_EXPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return ERROR_LOG_EXPORT_PATH
