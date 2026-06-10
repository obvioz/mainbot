from __future__ import annotations

MAX_TG_MESSAGE = 3900


def split_text(text: str, limit: int = MAX_TG_MESSAGE) -> list[str]:
    """Split long Telegram messages without cutting lines whenever possible."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        add_len = len(line) + 1
        if current and current_len + add_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def pct_emoji(value: float) -> str:
    if value >= 5:
        return "🟢"
    if value >= 0:
        return "🟩"
    if value <= -8:
        return "🔴"
    if value <= -3:
        return "🟠"
    return "🟡"


def status_emoji(status: str) -> str:
    return {
        "STRONG_BUY": "🟢",
        "ACCUMULATION": "🟡",
        "WATCH": "⚪",
        "AVOID": "🔴",
        "NO_SIGNAL": "⚪",
        "ERROR": "⚠️",
    }.get(status, "⚪")


def hr() -> str:
    return "━━━━━━━━━━━━"
