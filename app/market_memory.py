"""Persistent market memory.

Stores scan snapshots to data/market_snapshots.jsonl so the bot remembers
prices/scores/actions across restarts. This enables comparisons such as:
ETH since previous scan: price -1.2%, score +4, action WATCH -> ACCUMULATION.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage import DATA_DIR
from app.signals import classify_signal
from app.formatters import fmt_usdt

SNAPSHOT_PATH = DATA_DIR / "market_snapshots.jsonl"
MAX_SNAPSHOTS = 800


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def load_snapshots(limit: int | None = None) -> list[dict[str, Any]]:
    rows = _read_jsonl(SNAPSHOT_PATH)
    return rows[-limit:] if limit else rows


def latest_snapshot() -> dict[str, Any] | None:
    rows = load_snapshots(1)
    return rows[-1] if rows else None


def save_snapshot(snapshot: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_snapshots()
    rows.append(snapshot)
    rows = rows[-MAX_SNAPSHOTS:]
    SNAPSHOT_PATH.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8")


def build_snapshot(items: list[dict[str, Any]], market_context: dict[str, Any] | None = None) -> dict[str, Any]:
    btc = next((x for x in items if x.get("coin") == "BTC" and "error" not in x), None)
    coins: dict[str, Any] = {}
    for item in items:
        if "error" in item:
            coins[item.get("coin", "?")] = {"error": item.get("error")}
            continue
        sig = classify_signal(item, btc, market_context or {})
        coins[item["coin"]] = {
            "price": float(item.get("current", 0) or 0),
            "score": int(sig.get("score", 0) or 0),
            "status": sig.get("status", "WATCH"),
            "drawdown_30d_high": float(item.get("drawdown_30d_high", 0) or 0),
            "change_24h": float(item.get("change_24h", 0) or 0),
            "change_7d": float(item.get("change_7d", 0) or 0),
            "price_state": (sig.get("price_attractiveness") or {}).get("state"),
            "strength": (sig.get("strength_vs_btc") or {}).get("state"),
        }
    return {
        "ts": _now_iso(),
        "market_score": int((market_context or {}).get("score", 50) or 50),
        "market_mode": (market_context or {}).get("mode", "UNKNOWN"),
        "coins": coins,
    }


def record_scan_snapshot(items: list[dict[str, Any]], market_context: dict[str, Any] | None = None) -> dict[str, Any]:
    snap = build_snapshot(items, market_context)
    save_snapshot(snap)
    return snap


def _pct_change(now: float, old: float) -> float | None:
    if not old:
        return None
    return (now / old - 1.0) * 100.0


def snapshot_delta(current: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or _previous_snapshot_before(current)
    if not previous:
        return {"has_previous": False, "rows": []}
    rows = []
    cur_coins = current.get("coins", {})
    prev_coins = previous.get("coins", {})
    for coin, cur in cur_coins.items():
        prev = prev_coins.get(coin)
        if not prev or cur.get("error") or prev.get("error"):
            continue
        price_delta = _pct_change(float(cur.get("price", 0) or 0), float(prev.get("price", 0) or 0))
        score_delta = int(cur.get("score", 0) or 0) - int(prev.get("score", 0) or 0)
        status_old = prev.get("status", "?")
        status_new = cur.get("status", "?")
        rows.append({
            "coin": coin,
            "price": float(cur.get("price", 0) or 0),
            "price_delta_pct": price_delta,
            "score": int(cur.get("score", 0) or 0),
            "score_delta": score_delta,
            "status_old": status_old,
            "status_new": status_new,
            "changed_status": status_old != status_new,
        })
    rows.sort(key=lambda r: (abs(r.get("score_delta", 0)), abs(r.get("price_delta_pct") or 0)), reverse=True)
    return {"has_previous": True, "previous_ts": previous.get("ts"), "current_ts": current.get("ts"), "rows": rows}


def _previous_snapshot_before(current: dict[str, Any]) -> dict[str, Any] | None:
    rows = load_snapshots()
    if len(rows) < 2:
        return None
    # If current is already saved as last, previous is -2.
    if rows[-1].get("ts") == current.get("ts"):
        return rows[-2]
    return rows[-1]


def format_delta_report(delta: dict[str, Any], limit: int = 12) -> str:
    if not delta.get("has_previous"):
        return "📈 ИЗМЕНЕНИЯ\n\nПока нет предыдущего скана для сравнения. Сделай /scan еще раз позже."
    lines = [
        "📈 ИЗМЕНЕНИЯ С ПРОШЛОГО СКАНА",
        f"Предыдущий: {delta.get('previous_ts', '?')}",
        f"Текущий: {delta.get('current_ts', '?')}",
        "",
    ]
    rows = delta.get("rows", [])[:limit]
    if not rows:
        lines.append("Нет данных для сравнения.")
        return "\n".join(lines)
    for r in rows:
        pd = r.get("price_delta_pct")
        pd_txt = "?" if pd is None else f"{pd:+.2f}%"
        status = r.get("status_new")
        old = r.get("status_old")
        arrow = f" {old} → {status}" if r.get("changed_status") else f" {status}"
        lines.append(
            f"{r['coin']}: {fmt_usdt(r['price'])} | цена {pd_txt} | score {r['score']} ({r['score_delta']:+d}) |{arrow}"
        )
    return "\n".join(lines)


def format_memory_status() -> str:
    rows = load_snapshots()
    if not rows:
        return "🧠 ПАМЯТЬ РЫНКА\n\nСнимков пока нет. Сделай /scan."
    latest = rows[-1]
    return (
        "🧠 ПАМЯТЬ РЫНКА\n\n"
        f"Снимков сохранено: {len(rows)}\n"
        f"Последний скан: {latest.get('ts', '?')}\n"
        f"Режим: {latest.get('market_mode', '?')}\n"
        f"Market Score: {latest.get('market_score', '?')}/100"
    )
