import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.market import analyze_market, make_exchange, fetch_ohlcv_df
from app.signals import classify_signal, format_compact_signal, format_core_signal
from app.settings import settings
from app.storage import get_position, update_portfolio
from app.formatters import fmt_usdt, fmt_money
from app.strategy_params import get_tp_levels
from app.config import CATEGORIES
from app.error_log import log_error
from app.spot_journal import log_spot_sell
from app.market_intelligence import build_market_context
from app.bybit_portfolio_monitor import check_bybit_portfolio_changes
from app.rotation_lab import rotation_tick
from app.futures_lab import futures_tick, format_futures_event

STATE_PATH = Path("data/monitor_state.json")
SIGNAL_STATUSES = {"STRONG_BUY", "ACCUMULATION"}

ROTATION_INTERVAL_SECONDS = 5 * 60
FUTURES_INTERVAL_SECONDS = 5 * 60

TRAILING_TP1_PCT = 9.0      # % роста от средней для активации трейлинга
TRAILING_TP1_SHARE = 0.40   # доля позиции, которую рекомендуем закрыть на TP1

MIN_SIGNAL_SCORE = 70       # сигналы ниже этого score не отправляются
MIN_ENTRY_USDT = 10.0       # минимальный размер транша для нового входа
SIGNAL_COOLDOWN_SECONDS = 6 * 3600  # кулдаун повторного сигнала по одной монете
ADVISORY_TP_COOLDOWN_SECONDS = 6 * 3600  # advisory-напоминание о ручной продаже не чаще раза в 6ч


def signal_keyboard(coin: str, entry_usdt: float = 0) -> InlineKeyboardMarkup:
    amount_label = f"${entry_usdt:.0f}" if entry_usdt else "?"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Купить {amount_label}", callback_data=f"buy_now:{coin}:{entry_usdt:.2f}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip"),
        ],
    ])


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def signal_key(item: dict, signal: dict) -> str:
    coin = item.get("coin", "?")
    price = item.get("current", 0)
    rounded_price = round(float(price), 2) if price else 0
    return f"{coin}:{signal.get('status')}:{rounded_price}"


CORE_COINS = {"BTC", "ETH"}


def build_monitor_signals(items: list[dict], market_context: dict | None = None) -> list[tuple[str, str, float]]:
    btc = next((x for x in items if x.get("coin") == "BTC"), None)
    if market_context is None:
        market_context = build_market_context(items)
    state = load_state()
    now_ts = time.time()
    messages: list[tuple[str, str, float]] = []

    for item in items:
        if "error" in item:
            continue
        signal = classify_signal(item, btc, market_context)
        status = signal.get("status")
        coin = item.get("coin", "?")
        if status not in SIGNAL_STATUSES:
            state.pop(f"signal_{coin}", None)
            continue

        # Фильтр слабых сигналов (БАГ 2)
        score = int(signal.get("score", 0))
        if score < MIN_SIGNAL_SCORE:
            state.pop(f"signal_{coin}", None)
            continue

        entry_usdt = float(signal.get("entry_usdt") or 0)

        # Фильтр малого транша для новых входов (БАГ 1)
        pos = get_position(coin)
        if not pos and entry_usdt < MIN_ENTRY_USDT:
            state.pop(f"signal_{coin}", None)
            continue

        # Дедупликация: один сигнал по монете не чаще раза в 6 часов
        prev = state.get(f"signal_{coin}")
        if isinstance(prev, dict):
            if now_ts - prev.get("sent_at", 0) < SIGNAL_COOLDOWN_SECONDS:
                continue
        state[f"signal_{coin}"] = {"key": signal_key(item, signal), "sent_at": now_ts}

        if coin in CORE_COINS:
            text = format_core_signal(item, signal)
        else:
            text = format_compact_signal(item, signal)
        messages.append((text, coin, entry_usdt))

    state["_last_scan"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return messages


def _hold_hours_from_pos(pos: dict) -> float:
    """Estimate how many hours the position has been open from first entry date."""
    entries = pos.get("entries") or []
    if not entries:
        return 0.0
    first_ts = entries[0].get("date", "")
    if not first_ts:
        return 0.0
    try:
        first_dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        now = datetime.now(first_dt.tzinfo)
        return (now - first_dt).total_seconds() / 3600.0
    except Exception:
        return 0.0


def build_position_alerts(items: list[dict]) -> tuple[list[str], list[dict]]:
    """Scan open positions for DCA/TP/trailing triggers.

    Returns (messages, pending_executions).
    pending_executions — list of dicts describing sells to execute via Bybit:
        {type, coin, qty_sell, avg, price, pnl_pct, pnl_usdt, trailing_stop}

    The scan mutates portfolio_state.json (TP flags, trailing stops), so it runs
    inside update_portfolio() — an atomic read-modify-write under the global
    portfolio lock that prevents concurrent writers from clobbering each other.
    """
    return update_portfolio(lambda state: _scan_positions(state, items))


def _scan_positions(state: dict, items: list[dict]) -> tuple[list[str], list[dict]]:
    price_map = {x["coin"]: float(x["current"]) for x in items if "error" not in x}
    item_map = {x["coin"]: x for x in items if "error" not in x}
    positions = state.get("positions", {})
    alerts = state.setdefault("alerts", {})
    messages: list[str] = []
    pending_executions: list[dict] = []

    for coin, pos in list(positions.items()):
        if coin not in price_map:
            continue
        price = price_map[coin]
        avg = float(pos.get("avg_price", 0))
        next_buy = float(pos.get("next_buy_price", 0))
        qty = float(pos.get("qty", 0))
        invested = float(pos.get("invested_usdt", pos.get("invested_rub", 0)))
        value = qty * price
        pnl = value - invested
        pnl_pct = (price / avg - 1) * 100 if avg else 0

        if next_buy and price <= next_buy:
            key = f"buy_alert_{coin}_{next_buy}"
            if not alerts.get(key):
                alerts[key] = True
                messages.append(
                    f"🟡 ДОБОР ПО {coin}\n"
                    f"Цена сейчас: {fmt_usdt(price)} USDT\n"
                    f"Следующий уровень был: {fmt_usdt(next_buy)} USDT\n"
                    f"Средняя: {fmt_usdt(avg)} USDT | PnL {pnl_pct:+.2f}%\n"
                    f"Если докупил — введи: /buy {coin} сумма_USDT цена_покупки\n"
                    f"Пример: /buy {coin} 10 {fmt_usdt(price)}"
                )

        category = CATEGORIES.get(coin, "STRONG_ALT")
        tp_levels = get_tp_levels(coin)
        tp1 = float(tp_levels[0])
        tp2 = float(tp_levels[1])

        if avg and price >= avg * (1 + tp1 / 100) and not pos.get("take_profit_10_sent"):
            pos["take_profit_10_sent"] = True
            messages.append(
                f"🟢 ВРЕМЯ ФИКСАЦИИ {coin}\n"
                f"Цена сейчас: {fmt_usdt(price)} USDT\n"
                f"Средняя: {fmt_usdt(avg)} USDT\n"
                f"Позиция в плюсе: {pnl_pct:+.2f}% / {fmt_money(pnl)}\n"
                f"Первый TP для {category}: +{tp1:.0f}%\n"
                f"Можно зафиксировать 25–50% или закрыть: /sell {coin} all"
            )

        if avg and price >= avg * (1 + tp2 / 100) and not pos.get("take_profit_15_sent"):
            pos["take_profit_15_sent"] = True
            messages.append(
                f"🟢 СИЛЬНЫЙ ПЛЮС ПО {coin}\n"
                f"Цена сейчас: {fmt_usdt(price)} USDT\n"
                f"Средняя: {fmt_usdt(avg)} USDT\n"
                f"Позиция: {pnl_pct:+.2f}% / {fmt_money(pnl)}\n"
                f"Второй TP для {category}: +{tp2:.0f}%\n"
                f"Рекомендация системы: частичная фиксация прибыли."
            )

        # --- Трейлинг стоп: ТОЛЬКО детекция ---
        # C1/C2: здесь НЕ меняем qty/invested_usdt, НЕ ставим постоянных флагов
        # (trailing_tp1_done/trailing_active) и НЕ пишем в журнал. Любое изменение
        # денежного состояния и запись в журнал — только ПОСЛЕ подтверждённого
        # ордера, в monitor_loop → _process_sell_action → _apply_confirmed_sell.
        # Допустимо лишь безопасное обновление трейлинг-максимума (не денежная
        # операция), и только когда трейлинг уже активирован (после реальной TP1).
        if avg and qty > 0:
            item_data = item_map.get(coin, {})
            # 1h ATR аппроксимация: ATR_1d / sqrt(24)
            atr_pct_1d = float(item_data.get("atr_pct", 2.0))
            atr_1h_price = price * (atr_pct_1d / 100.0) / (24 ** 0.5)

            trailing_tp1_done = pos.get("trailing_tp1_done", False)
            trailing_active = pos.get("trailing_active", False)

            if not trailing_tp1_done and price >= avg * (1 + TRAILING_TP1_PCT / 100):
                # В advisory режиме не спамим: эмитим намерение не чаще кулдауна.
                emit = settings.tp_auto_execute or not _advisory_on_cooldown(pos, "TP1")
                if emit:
                    qty_sell_40 = qty * TRAILING_TP1_SHARE
                    pending_executions.append({
                        "type": "TP1",
                        "coin": coin,
                        "share": TRAILING_TP1_SHARE,
                        "qty_sell": qty_sell_40,
                        "avg": avg,
                        "price": price,
                        "pnl_pct": pnl_pct,
                        "pnl_usdt": qty_sell_40 * (price - avg),
                        "proposed_trailing_stop": round(price - atr_1h_price, 8),
                        "hold_hours": _hold_hours_from_pos(pos),
                    })

            elif trailing_active:
                # Обновление high-water mark — безопасно (не денежная операция).
                trailing_max = float(pos.get("trailing_max", price))
                trailing_stop_val = float(pos.get("trailing_stop", price * 0.97))

                if price > trailing_max:
                    new_stop = price - atr_1h_price
                    trailing_stop_val = max(trailing_stop_val, new_stop)
                    trailing_max = price
                    pos["trailing_max"] = trailing_max
                    pos["trailing_stop"] = round(trailing_stop_val, 8)

                if price <= trailing_stop_val:
                    qty_remaining = qty * (1 - TRAILING_TP1_SHARE)
                    pnl_from_avg = (price / avg - 1) * 100 if avg else 0
                    pending_executions.append({
                        "type": "trailing",
                        "coin": coin,
                        "share": 1 - TRAILING_TP1_SHARE,
                        "qty_sell": qty_remaining,
                        "avg": avg,
                        "price": price,
                        "pnl_pct": pnl_from_avg,
                        "pnl_usdt": qty_remaining * (price - avg),
                        "trailing_max": trailing_max,
                        "trailing_stop": trailing_stop_val,
                        "hold_hours": _hold_hours_from_pos(pos),
                    })

        positions[coin] = pos

    return messages, pending_executions


def _action_label(action: dict) -> str:
    return "TP1" if action.get("type") == "TP1" else "Трейлинг"


def _share_label(action: dict) -> str:
    return f"{int(round(float(action.get('share') or 0) * 100))}%"


def _exit_reason(action: dict) -> str:
    if action.get("type") == "TP1":
        return f"TP1 +{TRAILING_TP1_PCT:.0f}% от средней — зафиксировано {_share_label(action)}"
    return f"Трейлинг {fmt_usdt(action.get('trailing_stop') or 0)} USDT достигнут"


def _advisory_on_cooldown(pos: dict, action_type: str) -> bool:
    """True если advisory-напоминание этого типа отправлялось недавно.

    Используем кулдаун-таймстемп вместо постоянного флага: проверка TP никогда
    не отключается навсегда, просто не повторяется чаще раза в N часов.
    """
    cds = pos.get("advisory_cooldowns") or {}
    ts = cds.get(action_type)
    if not ts:
        return False
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
        return elapsed < ADVISORY_TP_COOLDOWN_SECONDS
    except Exception:
        return False


def _apply_advisory_cooldown(coin: str, action_type: str) -> None:
    """Отметить, что advisory-напоминание отправлено (ставит таймстемп, не флаг)."""
    def _mut(state: dict) -> None:
        pos = (state.get("positions") or {}).get(coin)
        if not pos:
            return
        pos.setdefault("advisory_cooldowns", {})[action_type] = datetime.now().isoformat(timespec="seconds")
    update_portfolio(_mut)


def _apply_confirmed_sell(coin: str, action: dict, exec_price: float, qty_done: float) -> dict | None:
    """Атомарно уменьшить позицию на ФАКТИЧЕСКИ проданный объём и выставить флаги.

    Вызывается ТОЛЬКО после ok=True от биржи. Возвращает обновлённый pos,
    {"closed": True} если позиция закрыта, либо None если позиции уже нет.
    """
    captured: dict = {}

    def _mut(state: dict) -> None:
        positions = state.setdefault("positions", {})
        pos = positions.get(coin)
        if not pos:
            captured["pos"] = None
            return

        old_qty = float(pos.get("qty", 0))
        sell_qty = min(float(qty_done), old_qty) if old_qty > 0 else 0.0
        share = sell_qty / old_qty if old_qty > 0 else 0.0
        cost = float(pos.get("invested_usdt", 0)) * share

        pos["qty"] = old_qty - sell_qty
        pos["invested_usdt"] = float(pos.get("invested_usdt", 0)) - cost
        # Средняя цена входа при продаже не меняется (cost basis на единицу тот же);
        # пересчитываем из остатка для согласованности.
        pos["avg_price"] = (
            pos["invested_usdt"] / pos["qty"] if pos["qty"] > 1e-12 else float(pos.get("avg_price", 0))
        )

        if action.get("type") == "TP1":
            pos["trailing_tp1_done"] = True
            pos["tp1_done"] = True
            pos["trailing_active"] = True
            pos["trailing_max"] = exec_price
            pos["trailing_stop"] = float(action.get("proposed_trailing_stop") or 0)
            pos["trailing_tp1_price"] = exec_price
        else:  # trailing — закрываем остаток
            pos["trailing_active"] = False
            pos["trailing_triggered"] = True

        if pos["qty"] <= 1e-12:
            positions.pop(coin, None)
            captured["pos"] = {"closed": True}
        else:
            positions[coin] = pos
            captured["pos"] = pos

    update_portfolio(_mut)
    return captured.get("pos")


def _reconcile_after_sell(coin: str) -> None:
    """TODO (H1+H2): после успешной продажи сверить локальный остаток с реальным
    балансом Bybit, который должен быть источником истины по qty при включённой
    реальной торговле. Пока заглушка — полная реализация в H1+H2."""
    return None


def _advisory_sell_message(action: dict) -> str:
    coin = action["coin"]
    head = (
        f"🟢 TP1 +{TRAILING_TP1_PCT:.0f}% — {coin}"
        if action.get("type") == "TP1"
        else f"🔴 Трейлинг — {coin}"
    )
    return (
        f"{head} (advisory)\n"
        f"Рекомендация: продать {_share_label(action)} вручную на Bybit\n"
        f"Средняя: {fmt_usdt(action['avg'])} USDT | Цена: {fmt_usdt(action['price'])} USDT\n"
        f"PnL: {float(action.get('pnl_pct') or 0):+.2f}% / {fmt_money(action.get('pnl_usdt') or 0)}\n"
        f"≈ {float(action['qty_sell']):.6f} {coin}\n"
        f"Автоторговля выключена — позиция НЕ изменена. "
        f"Повтор не раньше чем через {ADVISORY_TP_COOLDOWN_SECONDS // 3600}ч."
    )


def _confirmed_sell_message(action: dict, exec_price: float, qty_done: float) -> str:
    avg = float(action.get("avg") or 0)
    pnl_usdt = qty_done * (exec_price - avg) if avg else 0.0
    pnl_pct = (exec_price / avg - 1) * 100 if avg else 0.0
    msg = (
        f"✅ {_action_label(action)} исполнен — {action['coin']}\n"
        f"Продано {_share_label(action)} по ${fmt_usdt(exec_price)} ({qty_done:.6f} {action['coin']})\n"
        f"PnL: {fmt_money(pnl_usdt)} ({pnl_pct:+.1f}%)"
    )
    if action.get("type") == "TP1":
        msg += f"\nОстаток на трейлинге, стоп ${fmt_usdt(action.get('proposed_trailing_stop') or 0)}"
    else:
        msg += "\nПозиция закрыта."
    return msg


def _failed_sell_message(action: dict, result: dict) -> str:
    return (
        f"⚠️ {_action_label(action)} НЕ исполнен — {action['coin']}\n"
        f"Ошибка: {result.get('error') or 'неизвестная ошибка'}\n"
        f"Позиция НЕ изменена, проверь вручную.\n"
        f"Можно закрыть {_share_label(action)} (~{float(action['qty_sell']):.6f} {action['coin']}) "
        f"по рынку на Bybit."
    )


async def _process_sell_action(bot: Bot, chat_id: int, action: dict) -> None:
    """Денежная цепочка одной продажи (TP1/трейлинг).

    advisory: уведомить и поставить кулдаун, состояние НЕ трогать.
    auto: ордер → ТОЛЬКО при ok=True менять состояние/журнал; при ok=False —
    error_log + предупреждение, состояние не меняется.
    """
    coin = action["coin"]

    if not settings.tp_auto_execute:
        await bot.send_message(chat_id, _advisory_sell_message(action))
        try:
            _apply_advisory_cooldown(coin, action["type"])
        except Exception as exc:
            log_error("monitor.advisory_cooldown", exc, {"coin": coin, "type": action.get("type")})
        return

    from app.bybit_executor import execute_market_sell
    result = await asyncio.to_thread(
        execute_market_sell, coin, action["qty_sell"], action["type"]
    )

    if not result.get("ok"):
        log_error(
            "monitor.tp_execute",
            RuntimeError(result.get("error") or "order failed"),
            {"coin": coin, "type": action.get("type"), "qty_sell": action.get("qty_sell"), "result": result},
        )
        await bot.send_message(chat_id, _failed_sell_message(action, result))
        return

    # ok=True — фактические цифры из ответа Bybit
    exec_price = float(result.get("price") or action["price"])
    qty_done = float(result.get("qty_executed") or action["qty_sell"])

    applied = _apply_confirmed_sell(coin, action, exec_price, qty_done)
    if applied is None:
        log_error(
            "monitor.tp_apply",
            RuntimeError("position missing when applying confirmed sell"),
            {"coin": coin, "type": action.get("type")},
        )

    # Журнал — ФАКТИЧЕСКИЕ цифры, только после подтверждения ордера
    try:
        log_spot_sell(
            symbol=coin,
            exit_type=action["type"],
            entry_price_avg=float(action.get("avg") or 0),
            exit_price=exec_price,
            qty_sold=qty_done,
            pnl_pct=(exec_price / action["avg"] - 1) * 100 if action.get("avg") else 0.0,
            pnl_usdt=qty_done * (exec_price - action["avg"]) if action.get("avg") else 0.0,
            hold_hours=float(action.get("hold_hours") or 0.0),
            trailing_max_price=action.get("trailing_max") or action.get("price"),
            exit_reason=_exit_reason(action),
        )
    except Exception as exc:
        log_error("monitor.spot_journal", exc, {"coin": coin, "type": action.get("type")})

    await bot.send_message(chat_id, _confirmed_sell_message(action, exec_price, qty_done))

    # Источник истины по qty при реальной торговле — баланс Bybit (H1+H2).
    _reconcile_after_sell(coin)


def format_rotation_event(result: dict) -> str | None:
    """Красивое уведомление для Telegram по событиям Rotation Lab."""
    status = result.get("status")

    if status == "opened":
        return (
            "🧪 DEMO ENTRY\n\n"
            f"Пара: {result.get('pair')}\n"
            f"Score: {result.get('score', '?')}\n"
            f"1h: {float(result.get('change_1h') or 0):+.2f}%\n"
            f"3h: {float(result.get('change_3h') or 0):+.2f}%\n"
            f"Объём: x{float(result.get('volume_growth') or 0):.2f}\n"
            f"BTC demo: {float(result.get('virtual_btc') or 0):.8f}"
        )

    if status == "closed":
        return (
            "🧪 DEMO EXIT\n\n"
            f"Пара: {result.get('pair')}\n"
            f"Причина: {result.get('reason')}\n"
            f"Результат: {float(result.get('result_pct') or 0):+.2f}%\n"
            f"BTC: {float(result.get('btc_before') or 0):.8f} → "
            f"{float(result.get('btc_after') or 0):.8f}"
        )

    return None


async def monitor_loop(bot: Bot) -> None:
    if not settings.telegram_allowed_user_id:
        print("AUTO_MONITOR: TELEGRAM_ALLOWED_USER_ID не задан, фоновые сигналы отключены.")
        return

    chat_id = int(settings.telegram_allowed_user_id)
    interval_seconds = max(settings.scan_interval_minutes, 1) * 60

    await bot.send_message(
        chat_id,
        f"🟢 Автомониторинг v15 запущен.\n"
        f"Рынок: {settings.scan_interval_minutes} мин.\n"
        f"Bybit portfolio monitor: {'включен' if settings.bybit_portfolio_monitor_enabled else 'выключен'} "
        f"/ {settings.bybit_portfolio_monitor_minutes} мин.\n"
        f"Rotation Lab demo: каждые {ROTATION_INTERVAL_SECONDS // 60} мин.\n"
        f"Futures Lab demo: каждые {FUTURES_INTERVAL_SECONDS // 60} мин.\n\n"
        "Команды: /status /scan /positions /bybit /syncbybit /rotation /futures /circuitstatus"
    )

    last_market_scan = 0.0
    last_bybit_scan = 0.0
    last_rotation_scan = 0.0
    last_futures_scan = 0.0

    bybit_interval = max(settings.bybit_portfolio_monitor_minutes, 1) * 60

    while True:
        now = time.monotonic()

        if (
            settings.bybit_portfolio_monitor_enabled
            and settings.bybit_api_key
            and settings.bybit_api_secret
            and (now - last_bybit_scan >= bybit_interval)
        ):
            try:
                messages = await asyncio.to_thread(check_bybit_portfolio_changes, True)
                for text in messages:
                    await bot.send_message(chat_id, text)
                last_bybit_scan = now
            except Exception as exc:
                await bot.send_message(chat_id, f"⚠️ Ошибка Bybit-монитора: {exc}")
                last_bybit_scan = now

        # Rotation Lab demo tick
        if now - last_rotation_scan >= ROTATION_INTERVAL_SECONDS:
            try:
                result = await asyncio.to_thread(rotation_tick)
                text = format_rotation_event(result)
                if text:
                    await bot.send_message(chat_id, text)
                last_rotation_scan = now
            except Exception as exc:
                await bot.send_message(chat_id, f"⚠️ Ошибка Rotation Lab: {exc}")
                last_rotation_scan = now

        # Futures Lab demo tick
        if now - last_futures_scan >= FUTURES_INTERVAL_SECONDS:
            try:
                result = await asyncio.to_thread(futures_tick)
                text = format_futures_event(result.get("event"))
                if text:
                    await bot.send_message(chat_id, text)
                for note in result.get("notifications") or []:
                    await bot.send_message(chat_id, note)
                last_futures_scan = now
            except Exception as exc:
                await bot.send_message(chat_id, f"⚠️ Ошибка Futures Lab: {exc}")
                last_futures_scan = now

        if now - last_market_scan >= interval_seconds:
            try:
                items = await asyncio.to_thread(analyze_market)
                exchange = make_exchange()
                market_context = await asyncio.to_thread(build_market_context, items, exchange, fetch_ohlcv_df)
                signal_messages = build_monitor_signals(items, market_context)
                for text, coin, entry_usdt in signal_messages:
                    kb = signal_keyboard(coin, entry_usdt) if coin not in CORE_COINS else None
                    await bot.send_message(chat_id, text, reply_markup=kb)
                alert_msgs, pending_execs = build_position_alerts(items)
                for text in alert_msgs:
                    await bot.send_message(chat_id, text)
                for action in pending_execs:
                    await _process_sell_action(bot, chat_id, action)
                last_market_scan = now
            except Exception as exc:
                await bot.send_message(chat_id, f"⚠️ Ошибка автомониторинга: {exc}")
                last_market_scan = now

        await asyncio.sleep(30)
