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
from app.storage import load_portfolio, save_portfolio
from app.formatters import fmt_usdt, fmt_money
from app.strategy_params import get_tp_levels
from app.config import CATEGORIES
from app.market_intelligence import build_market_context
from app.bybit_portfolio_monitor import check_bybit_portfolio_changes
from app.rotation_lab import rotation_tick

STATE_PATH = Path("data/monitor_state.json")
SIGNAL_STATUSES = {"STRONG_BUY", "ACCUMULATION"}

# Rotation Lab пока работает только в demo/simulation режиме.
ROTATION_INTERVAL_SECONDS = 5 * 60


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
        key = signal_key(item, signal)
        if state.get(f"signal_{coin}") == key:
            continue
        state[f"signal_{coin}"] = key
        entry_usdt = float(signal.get("entry_usdt") or 0)
        if coin in CORE_COINS:
            text = format_core_signal(item, signal)
        else:
            text = format_compact_signal(item, signal)
        messages.append((text, coin, entry_usdt))

    state["_last_scan"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return messages


def build_position_alerts(items: list[dict]) -> list[str]:
    price_map = {x["coin"]: float(x["current"]) for x in items if "error" not in x}
    state = load_portfolio()
    positions = state.get("positions", {})
    alerts = state.setdefault("alerts", {})
    messages = []

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

        positions[coin] = pos

    save_portfolio(state)
    return messages


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
        f"Rotation Lab demo: включен / каждые {ROTATION_INTERVAL_SECONDS // 60} мин.\n\n"
        "Команды: /status /scan /positions /bybit /syncbybit /rotation"
    )

    last_market_scan = 0.0
    last_bybit_scan = 0.0
    last_rotation_scan = 0.0

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

        if now - last_market_scan >= interval_seconds:
            try:
                items = await asyncio.to_thread(analyze_market)
                exchange = make_exchange()
                market_context = await asyncio.to_thread(build_market_context, items, exchange, fetch_ohlcv_df)
                signal_messages = build_monitor_signals(items, market_context)
                for text, coin, entry_usdt in signal_messages:
                    kb = signal_keyboard(coin, entry_usdt) if coin not in CORE_COINS else None
                    await bot.send_message(chat_id, text, reply_markup=kb)
                for text in build_position_alerts(items):
                    await bot.send_message(chat_id, text)
                last_market_scan = now
            except Exception as exc:
                await bot.send_message(chat_id, f"⚠️ Ошибка автомониторинга: {exc}")
                last_market_scan = now

        await asyncio.sleep(30)
