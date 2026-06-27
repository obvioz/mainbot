from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.bybit_sync import build_bybit_assets, sync_bybit_to_local
from app.formatters import fmt_money, fmt_usdt
from app.storage import now_iso

STATE_PATH = Path("data/bybit_portfolio_monitor.json")

# Не уведомлять о продаже, если остаток монеты после продажи ниже этого порога (пыль)
MIN_SELL_NOTIFY_USDT = 1.0


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {"assets": {}, "initialized": False}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"assets": {}, "initialized": False}


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _asset_snapshot() -> tuple[dict[str, dict[str, Any]], dict]:
    assets, quote = build_bybit_assets()
    snap: dict[str, dict[str, Any]] = {}
    for a in assets:
        snap[a.coin] = {
            "coin": a.coin,
            "qty": float(a.qty),
            "avg_price": float(a.avg_price or 0),
            "invested_usdt": float(a.invested_usdt or 0),
            "current_price": float(a.current_price or 0),
            "current_value": float(a.current_value or 0),
            "pnl_pct": float(a.pnl_pct or 0),
            "pnl_usdt": float(a.pnl_usdt or 0),
            "source": a.source,
        }
    return snap, quote


def _pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new / old - 1) * 100


def _position_line(cur: dict) -> str:
    coin = cur["coin"]
    pnl = float(cur.get("pnl_pct") or 0)
    emoji = "🟢" if pnl >= 0 else "🔴"
    return (
        f"{emoji} {coin}: {float(cur.get('qty') or 0):.8f}\n"
        f"Средняя: {fmt_usdt(cur.get('avg_price') or 0)} USDT\n"
        f"Текущая: {fmt_usdt(cur.get('current_price') or 0)} USDT\n"
        f"PnL: {pnl:+.2f}% / {fmt_money(cur.get('pnl_usdt') or 0)}"
    )


def check_bybit_portfolio_changes(sync_local: bool = True) -> list[str]:
    """Read Bybit, compare with last snapshot, optionally sync local portfolio.

    Returns user-facing alert messages. Does not place orders.
    """
    state = _read_state()
    prev_assets: dict[str, dict] = state.get("assets") or {}
    initialized = bool(state.get("initialized"))

    current_assets, quote = _asset_snapshot()
    messages: list[str] = []

    if not initialized:
        _write_state({
            "initialized": True,
            "assets": current_assets,
            "quote": quote,
            "updated_at": now_iso(),
        })
        if sync_local:
            sync_bybit_to_local()
        return [
            "🔗 Bybit-монитор портфеля включен.\n"
            "Первый снимок сохранен, дальше буду сообщать о новых покупках, продажах, DCA и TP."
        ]

    # New / increased / decreased / closed positions
    all_coins = sorted(set(prev_assets.keys()) | set(current_assets.keys()))
    for coin in all_coins:
        prev = prev_assets.get(coin)
        cur = current_assets.get(coin)
        if cur and not prev:
            messages.append(
                "🟢 Обнаружена новая позиция на Bybit\n\n"
                f"{_position_line(cur)}\n\n"
                "Локальный портфель будет обновлен."
            )
            continue
        if prev and not cur:
            # Полная продажа: уведомляем только если позиция была заметной (> порога)
            if float(prev.get("current_value") or 0) >= MIN_SELL_NOTIFY_USDT:
                messages.append(
                    "✅ Позиция исчезла с Bybit\n\n"
                    f"{coin}\n"
                    "Вероятно, позиция закрыта. Локальный портфель будет обновлен."
                )
            continue
        if not prev or not cur:
            continue

        prev_qty = float(prev.get("qty") or 0)
        cur_qty = float(cur.get("qty") or 0)
        if prev_qty <= 0:
            continue
        change_pct = _pct_change(cur_qty, prev_qty)

        if change_pct > 0.5:
            messages.append(
                "🟢 Обнаружена докупка на Bybit\n\n"
                f"{coin}\n"
                f"Кол-во: {prev_qty:.8f} → {cur_qty:.8f} ({change_pct:+.2f}%)\n"
                f"Средняя сейчас: {fmt_usdt(cur.get('avg_price') or 0)} USDT\n"
                f"PnL: {float(cur.get('pnl_pct') or 0):+.2f}%\n\n"
                "Локальный портфель будет обновлен, DCA пересчитается."
            )
        elif change_pct < -0.5:
            # Не уведомлять, если остаток после продажи — пыль (< порога)
            if float(cur.get("current_value") or 0) < MIN_SELL_NOTIFY_USDT:
                continue
            messages.append(
                "🔵 Обнаружена продажа на Bybit\n\n"
                f"{coin}\n"
                f"Кол-во: {prev_qty:.8f} → {cur_qty:.8f} ({change_pct:+.2f}%)\n"
                f"Остаток: {fmt_money(cur.get('current_value') or 0)}\n"
                f"Текущий PnL остатка: {float(cur.get('pnl_pct') or 0):+.2f}%\n\n"
                "Локальный портфель будет обновлен."
            )

    # Уведомления о фиксации прибыли (TP) намеренно НЕ шлём отсюда: единственный
    # источник TP-алертов — advisory TP1 +9% в monitor.py (с cooldown и фильтром
    # пыли). Этот модуль отвечает только за детекцию покупок/продаж и синк.

    if sync_local:
        try:
            sync_bybit_to_local()
        except Exception as exc:
            messages.append(f"⚠️ Bybit прочитан, но локальная синхронизация не удалась: {exc}")

    _write_state({
        "initialized": True,
        "assets": current_assets,
        "quote": quote,
        "updated_at": now_iso(),
    })
    return messages
