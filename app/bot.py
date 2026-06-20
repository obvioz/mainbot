import asyncio
import re
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, ErrorEvent
from aiogram.exceptions import TelegramBadRequest


from app.settings import settings
from app.config import normalize_coin
from app.market import analyze_market, make_exchange, analyze_coin, fetch_ohlcv_df
from app.derivatives import short_derivatives_text
from app.news_risk import short_news_text
from app.market_intelligence import build_market_context
from app.signals import format_signal_report, classify_signal, format_compact_signal, get_market_regime
from app.portfolio import format_portfolio_plan
from app.monitor import monitor_loop, STATE_PATH
from app.storage import record_buy, record_sell, get_open_positions, export_journal_csv, get_position
from app.formatters import format_positions_report, format_journal, format_stats_report, format_risk_report, format_buy_risk_warning, fmt_usdt, fmt_money
from app.ui import split_text
from app.risk_manager import check_buy_risk
from app.coin_health import health_check, blocks_entry
from app.position_quality import position_quality_score, format_position_quality
from app.bybit_sync import format_bybit_portfolio, sync_bybit_to_local
from app.bybit_portfolio_monitor import check_bybit_portfolio_changes
from app.backtest import backtest_coin, backtest_all, format_backtest_coin_report, format_backtest_all_report, DEFAULT_START, DEFAULT_END
from app.system_log import add_system_event, add_user_note, build_context_summary, export_context_markdown
from app.error_log import log_error, format_error_summary, export_errors_markdown
from app.ml_dataset import export_ml_dataset, format_ml_dataset_summary
from app.strategy_lab import (
    run_strategy_lab, format_strategy_lab_report, five_year_start,
    run_entry_levels, format_entry_levels_report, get_entry_levels,
)
from app.strategy_robustness import run_robustness_lab, format_robustness_report, format_reliability_report
from app.strategy_params import format_strategy_params
from app.spot_journal import (
    log_spot_buy, log_spot_sell,
    format_spot_journal, format_spot_report, export_spot_csv,
)
from app.bootstrap import auto_strategy_bootstrap
from app.market_cache import get_cached
from app.market_memory import record_scan_snapshot, latest_snapshot, snapshot_delta, format_delta_report, format_memory_status
from app.rotation_lab import (
    rotation_tick,
    rotation_summary,
    rotation_history,
    rotation_reset,
    rotation_report,
    export_rotation_csv,
)
from app.futures_lab import (
    futures_tick,
    futures_summary,
    futures_report,
    export_futures_csv,
    futures_reset,
    futures_set_enabled,
    circuit_status,
    load_futures_state,
)
from app.pro_lab import (
    pro_summary,
    pro_report,
    export_pro_csv,
    battle_report,
    load_pro_state,
)
from app.rotation_lab import load_rotation_state

PENDING_BUY: dict[int, str] = {}
SCAN_LOCK = asyncio.Lock()


def cached_analyze_market(force: bool = False):
    return get_cached("analyze_market", settings.market_cache_ttl_seconds, analyze_market, force=force)


def cached_market_context(items):
    def factory():
        exchange = make_exchange()
        return build_market_context(items, exchange, fetch_ohlcv_df)
    return get_cached("market_context", settings.market_context_cache_ttl_seconds, factory)


def is_allowed(message_or_call) -> bool:
    user = message_or_call.from_user
    if not settings.telegram_allowed_user_id:
        return True
    return str(user.id) == str(settings.telegram_allowed_user_id)


def signal_keyboard(coin: str, entry_usdt: float = 0) -> InlineKeyboardMarkup:
    amount_label = f"${entry_usdt:.0f}" if entry_usdt else "?"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Купить {amount_label}", callback_data=f"buy_now:{coin}:{entry_usdt:.2f}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip"),
        ],
    ])


def main_keyboard() -> ReplyKeyboardMarkup:
    """Чистая постоянная клавиатура.

    Убраны устаревшие кнопки ручной покупки/продажи и редко используемые действия.
    Основная логика теперь: Bybit = источник истины, бот = аналитик/монитор/журнал.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Скан"), KeyboardButton(text="🌍 Рынок")],
            [KeyboardButton(text="💼 Позиции"), KeyboardButton(text="📊 Активные сделки")],
            [KeyboardButton(text="🔗 Bybit"), KeyboardButton(text="🧠 Review")],
            [KeyboardButton(text="🛡️ Риски"), KeyboardButton(text="📈 Изменения")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
        is_persistent=True,
    )


async def get_current_price(coin: str) -> float:
    exchange = make_exchange()
    item = await asyncio.to_thread(analyze_coin, exchange, coin.upper())
    return float(item["current"])


async def build_price_map() -> dict[str, float]:
    items = await asyncio.to_thread(cached_analyze_market)
    return {x["coin"]: float(x["current"]) for x in items if "error" not in x}


def parse_buy_args(parts: list[str]) -> tuple[str, float, float]:
    if len(parts) < 4:
        raise ValueError("Формат: /buy ETH 10 2178.5")
    coin = normalize_coin(parts[1])
    amount_usdt = float(parts[2].replace(",", "."))
    price = float(parts[3].replace(",", "."))
    return coin, amount_usdt, price


def help_text() -> str:
    return (
        "🤖 Crypto Invest Bot v25 — Market Memory + Performance\n\n"
        "Главная логика: Bybit = источник истины. Покупки и продажи лучше делать на Bybit, "
        "а бот сам увидит портфель через read-only API и будет подсказывать доборы/фиксацию.\n\n"
        "Основные команды:\n"
        "/scan — решение по всем монетам: STRONG BUY / ACCUMULATION / WATCH / AVOID\n"
        "/status — режим рынка\n"
        "/positions — локальные открытые позиции\n"
        "/bybit — реальный портфель Bybit\n"
        "/bybitcheck — проверить новые покупки/продажи на Bybit\n"
        "/review — оценка открытых позиций\n"
        "/stats — статистика стратегии\n"
        "/risk — риск и концентрация портфеля\n"
        "/backtest ETH — тест стратегии на истории\n"
        "/backtestall — тест всех монет\n"
        "/mldataset — собрать CSV для ML\n"
        "/exportlog — выгрузить журнал контекста для ChatGPT\n"
        "/errors и /exporterrors — журнал ошибок\n\n"
        "Старые команды /buy и /sell оставлены как аварийный ручной режим, "
        "но в обычной работе лучше использовать Bybit Sync/Monitor."
    )


async def cmd_start(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(help_text(), reply_markup=main_keyboard())


async def cmd_help(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(help_text(), reply_markup=main_keyboard())


async def cmd_portfolio(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(format_portfolio_plan())


async def cmd_positions(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        price_map = await build_price_map()
        await message.answer(format_positions_report(price_map))
    except Exception as exc:
        await message.answer(f"Ошибка портфеля: {exc}")


async def cmd_bybit(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        await message.answer("Читаю портфель Bybit...")
        report = await asyncio.to_thread(format_bybit_portfolio)
        for chunk in split_text(report):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(
            f"Не смог прочитать Bybit: {exc}\n\n"
            "Проверь .env:\n"
            "BYBIT_API_KEY=...\n"
            "BYBIT_API_SECRET=...\n\n"
            "Ключ должен быть READ ONLY. Trade и Withdraw не включать."
        )


async def cmd_syncbybit(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        await message.answer("Синхронизирую локальный портфель с Bybit read-only...")
        res = await asyncio.to_thread(sync_bybit_to_local)
        synced = res.get("synced", [])
        skipped = res.get("skipped", [])
        lines = ["🔄 BYBIT SYNC"]
        if synced:
            lines.append(f"✅ Синхронизировано: {', '.join(synced)}")
        else:
            lines.append("Синхронизированных монет нет.")
        if skipped:
            lines.append("\n⚠️ Пропущено, потому что не удалось определить среднюю цену:")
            for row in skipped:
                lines.append(f"• {row.get('coin')}: {row.get('reason')}")
        lines.append("\nТеперь /positions, /review и /scan будут опираться на обновленный локальный портфель.")
        await message.answer("\n".join(lines))
    except Exception as exc:
        await message.answer(
            f"Не смог синхронизировать Bybit: {exc}\n\n"
            "Проверь, что API ключ read-only и добавлен в .env."
        )


async def cmd_bybitcheck(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        await message.answer("Проверяю изменения портфеля Bybit...")
        messages = await asyncio.to_thread(check_bybit_portfolio_changes, True)
        if not messages:
            await message.answer("👁️ Изменений по Bybit-портфелю не найдено. Локальный портфель синхронизирован.")
        else:
            for text in messages:
                await message.answer(text)
    except Exception as exc:
        await message.answer(f"Ошибка Bybit Check: {exc}")


async def cmd_risk(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        price_map = await build_price_map()
        await message.answer(format_risk_report(price_map))
    except Exception as exc:
        await message.answer(f"Ошибка риск-менеджера: {exc}")


async def cmd_journal(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(format_journal())


async def cmd_scan(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    if SCAN_LOCK.locked():
        await message.answer("Скан уже выполняется. Дождись результата, чтобы не перегружать Bybit/Telegram.")
        return
    await message.answer("Сканирую рынок... Если данные недавно обновлялись, использую кэш.")
    async with SCAN_LOCK:
        try:
            prev_snapshot = latest_snapshot()
            items = await asyncio.to_thread(cached_analyze_market)
            market_context = await asyncio.to_thread(cached_market_context, items)
            report = format_signal_report(items, market_context)

            current_snapshot = None
            if settings.save_market_snapshots:
                try:
                    current_snapshot = record_scan_snapshot(items, market_context)
                except Exception as mem_exc:
                    try:
                        log_error(mem_exc, source="market_memory")
                    except Exception:
                        pass

            try:
                add_system_event("SCAN", "Ручной скан рынка", data={"market_score": market_context.get("score"), "mode": market_context.get("mode"), "reasons": market_context.get("reasons", [])[:5]}, text=report)
            except Exception:
                pass
            for chunk in split_text(report):
                await message.answer(chunk)

            if current_snapshot and prev_snapshot:
                delta_text = format_delta_report(snapshot_delta(current_snapshot, prev_snapshot), limit=6)
                await message.answer(delta_text)

            btc = next((x for x in items if x.get("coin") == "BTC"), None)
            sent_cards = 0
            for item in items:
                if "error" in item:
                    continue
                signal = classify_signal(item, btc, market_context)
                if signal.get("status") in {"STRONG_BUY", "ACCUMULATION"} and sent_cards < 5:
                    entry_usdt = float(signal.get("entry_usdt") or 0)
                    await message.answer(format_compact_signal(item, signal), reply_markup=signal_keyboard(item["coin"], entry_usdt))
                    sent_cards += 1
        except Exception as exc:
            try:
                log_error(exc, source="cmd_scan")
            except Exception:
                pass
            await message.answer(f"Ошибка скана: {exc}")


async def cmd_changes(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    rows = []
    try:
        from app.market_memory import load_snapshots
        rows = load_snapshots(2)
    except Exception:
        rows = []
    if len(rows) < 2:
        await message.answer("Пока нет двух сканов для сравнения. Сделай /scan сейчас и еще раз позже.")
        return
    await message.answer(format_delta_report(snapshot_delta(rows[-1], rows[-2]), limit=12))


async def cmd_memory(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(format_memory_status())


def check_buy_allowed(coin: str, amount_usdt: float, price: float | None) -> tuple[bool, str, dict]:
    """Единая точка проверки риска перед ЛЮБОЙ записью покупки.

    Используется всеми путями входа (текстовая /buy, кнопка «✅ Купить», ручной
    ввод суммы), чтобы жёсткий лимит max_entries нельзя было обойти.

    Возвращает (allowed, message, risk_check):
      - allowed=False → message содержит причину отказа (готово для ответа юзеру);
      - allowed=True  → message содержит мягкие предупреждения (можно показать).
    """
    risk_check = check_buy_risk(coin, amount_usdt, price)
    if not risk_check["allowed"]:
        cr = risk_check.get("coin_risk") or {}
        entry_count = int(cr.get("entry_count", 0) or 0)
        max_entries = int(cr.get("max_entries", 0) or 0)
        reason = "; ".join(risk_check.get("blockers") or ["превышен лимит риска"])
        message = (
            f"⚠️ Вход отклонён: {reason}.\n"
            f"Транш {entry_count} из {max_entries} уже использованы."
        )
        return False, message, risk_check

    # Health-check монеты по данным биржи (статус пары, ликвидность, аномалия цены,
    # спред). Это жёсткий фильтр здоровья ВМЕСТО ненадёжного news_risk: DEAD/ANOMALY
    # блокируют вход, WARNING разрешает с пометкой.
    try:
        health = health_check(coin)
    except Exception as exc:
        health = {"status": "UNKNOWN", "reasons": [f"health-check недоступен: {exc}"], "details": {}}
    risk_check["health"] = health

    if blocks_entry(health):
        reason = "; ".join(health.get("reasons") or ["здоровье монеты под вопросом"])
        return False, f"⚠️ Вход в {coin} заблокирован: {reason}", risk_check

    warn = format_buy_risk_warning(risk_check)
    status = health.get("status")
    if status == "WARNING":
        hwarn = "⚠️ Здоровье монеты: WARNING — " + "; ".join(health.get("reasons") or [])
        warn = (warn + "\n" + hwarn).strip() if warn else hwarn
    elif status == "UNKNOWN":
        note = "ℹ️ Здоровье монеты не проверено (биржа недоступна)"
        warn = (warn + "\n" + note).strip() if warn else note
    return True, warn, risk_check


async def cmd_buy(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = message.text.split()
    try:
        coin, amount_usdt, price = parse_buy_args(parts)
        allowed, risk_msg, risk_check = await asyncio.to_thread(check_buy_allowed, coin, amount_usdt, price)
        if not allowed:
            await message.answer(risk_msg)
            return
        try:
            market_item = await asyncio.to_thread(analyze_coin, make_exchange(), coin)
            dca_levels = market_item.get("dca_levels") or None
        except Exception:
            dca_levels = None
        pos = record_buy(coin, amount_usdt, price, reason="manual", dca_levels=dca_levels)
        try:
            add_system_event("BUY", f"Покупка {coin}", data={"coin": coin, "amount_usdt": amount_usdt, "price": price, "avg_price": pos.get("avg_price"), "entry_count": pos.get("entry_count"), "next_buy_price": pos.get("next_buy_price")})
        except Exception:
            pass
        # Spot journal: log buy with available indicators
        try:
            _mi = market_item if market_item else {}
            _deriv = (_mi.get("derivatives") or {})
            _news = (_mi.get("news_risk") or {})
            _dd90 = _mi.get("drawdown_90d_high")
            _levels = get_entry_levels(coin)
            _trigger, _trigger_pct = "manual", None
            if _levels and _dd90 is not None:
                _dd_abs = abs(float(_dd90))
                if _levels.get("t3") and _dd_abs >= float(_levels["t3"]):
                    _trigger, _trigger_pct = f"t3 (-{_levels['t3']}%)", float(_levels["t3"])
                elif _levels.get("t2") and _dd_abs >= float(_levels["t2"]):
                    _trigger, _trigger_pct = f"t2 (-{_levels['t2']}%)", float(_levels["t2"])
                elif _levels.get("t1") and _dd_abs >= float(_levels["t1"]):
                    _trigger, _trigger_pct = f"t1 (-{_levels['t1']}%)", float(_levels["t1"])
            _indicators = {
                "drawdown_pct": _dd90,
                "score": None,
                "volume_ratio": None,
                "funding": _deriv.get("funding_pct"),
                "news_risk": _news.get("state"),
                "volatility": _mi.get("volatility_class"),
                "health": (risk_check.get("health") or {}).get("status"),
            }
            log_spot_buy(
                symbol=coin,
                tranche=int(pos.get("entry_count", 1)),
                entry_price=price,
                amount_usdt=amount_usdt,
                qty=amount_usdt / price,
                trigger=_trigger,
                trigger_pct=_trigger_pct,
                auto=False,
                indicators=_indicators,
            )
        except Exception:
            pass
        risk_text = format_buy_risk_warning(risk_check)
        await message.answer(
            f"✅ Покупка записана\n\n"
            f"{coin}: {fmt_money(amount_usdt)} по {fmt_usdt(price)} USDT\n"
            f"Куплено: {amount_usdt / price:.6f} {coin}\n"
            f"Средняя цена: {fmt_usdt(pos['avg_price'])} USDT\n"
            f"Всего вложено: {fmt_money(pos['invested_usdt'])}\n"
            f"Количество входов: {pos['entry_count']}\n"
            f"Следующий добор: {fmt_usdt(pos['next_buy_price'])} USDT (-{pos.get('next_buy_drop_pct','?')}%)\n\n"
            f"{risk_text}"
        )
    except Exception as exc:
        await message.answer(
            f"Не смог записать покупку: {exc}\n\n"
            "Правильный формат:\n"
            "/buy ETH 10 2178.5\n\n"
            "Где 10 — сумма в USDT, 2178.5 — фактическая цена покупки."
        )


async def cmd_sell(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат продажи:\n\n"
            "/sell ETH all — продать всю позицию по текущей цене\n"
            "/sell ETH all 2300 — продать всю позицию по фактической цене 2300\n"
            "/sell ETH 5 2300 — продать примерно на 5 USDT по фактической цене 2300"
        )
        return

    coin = normalize_coin(parts[1])

    try:
        # Цена продажи: если пользователь указал 4-й аргумент, берем его.
        # Если нет — берем текущую цену с Bybit.
        manual_price = None
        if len(parts) >= 4:
            manual_price = float(parts[3].replace(",", "."))
        price = manual_price if manual_price else await get_current_price(coin)

        if parts[2].lower() in {"all", "все", "всё"}:
            res = record_sell(coin, price, sell_all=True)
        else:
            amount_usdt = float(parts[2].replace(",", "."))
            res = record_sell(coin, price, amount_usdt=amount_usdt, sell_all=False)

        source = "твоя цена" if manual_price else "текущая цена Bybit"
        try:
            add_system_event("SELL", f"Продажа {coin}", data=res)
        except Exception:
            pass
        await message.answer(
            f"🔵 Продажа записана\n\n"
            f"{coin}: {fmt_money(res['amount_usdt'])} по {fmt_usdt(price)} USDT ({source})\n"
            f"Продано: {res['qty']:.6f} {coin}\n"
            f"Результат: {res['pnl_pct']:+.2f}% / {fmt_money(res['pnl_usdt'])}\n"
            f"Позиция закрыта: {'да' if res['sell_all'] else 'нет'}"
        )
    except Exception as exc:
        await message.answer(f"Не смог записать продажу: {exc}")


async def cmd_status(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        items = await asyncio.to_thread(cached_analyze_market)
        btc = next((x for x in items if x.get("coin") == "BTC"), None)
        market_context = await asyncio.to_thread(cached_market_context, items)
        regime = get_market_regime(btc, market_context=market_context)
        open_positions = get_open_positions()
        btc_line = "BTC: нет данных"
        if btc and "error" not in btc:
            btc_line = f"BTC: {fmt_usdt(btc['current'])} USDT | 24ч {btc['change_24h']:+.2f}% | 7д {btc['change_7d']:+.2f}%"
        ctx = market_context
        fng = ctx.get("fear_greed", {})
        dom = ctx.get("btc_dominance", {})
        trend = ctx.get("btc_trend", {})
        alt = ctx.get("alt_basket", {})
        dom_txt = f"{float(dom.get('value', 0)):.1f}%" if float(dom.get('value', 0) or 0) else "нет данных"
        await message.answer(
            "🌍 СТАТУС РЫНКА v6\n\n"
            f"{btc_line}\n"
            f"Режим: {regime['name']}\n"
            f"Market Score: {regime['score']}/100\n"
            f"BTC trend: {trend.get('trend','UNKNOWN')}\n"
            f"Fear & Greed: {fng.get('value',50)} ({fng.get('classification','Neutral')})\n"
            f"BTC dominance: {dom_txt}\n"
            f"Альт-корзина: {alt.get('state','UNKNOWN')} ({alt.get('strong_count',0)}/{alt.get('total',0)} сильнее BTC)\n\n"
            f"Комментарий: {regime['comment']}\n"
            f"Факторы: {' | '.join(ctx.get('reasons', [])[:5])}\n\n"
            f"Открытых позиций: {len(open_positions)}\n"
            f"Автомониторинг: {'включен' if settings.auto_monitor_enabled else 'выключен'}"
        )
    except Exception as exc:
        await message.answer(f"Ошибка статуса: {exc}")


async def cmd_price(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /price ETH")
        return
    coin = normalize_coin(parts[1])
    try:
        item = await asyncio.to_thread(analyze_coin, make_exchange(), coin)
        await message.answer(
            f"💵 {coin}/USDT\n"
            f"Цена: {fmt_usdt(item['current'])} USDT\n"
            f"24ч: {item['change_24h']:+.2f}%\n"
            f"7д: {item['change_7d']:+.2f}%\n"
            f"От high30: {item['drawdown_30d_high']:+.2f}%\n"
            f"Vol: {item.get('volatility_class')} | ATR14: {item.get('atr_pct',0):.2f}%"
        )
    except Exception as exc:
        await message.answer(f"Не смог получить цену {coin}: {exc}")


async def cmd_funding(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /funding ETH")
        return
    coin = normalize_coin(parts[1])
    try:
        item = await asyncio.to_thread(analyze_coin, make_exchange(), coin)
        d = item.get("derivatives") or {}
        funding_pct = d.get("funding_pct")
        funding_text = "нет данных" if funding_pct is None else f"{float(funding_pct):+.4f}%"
        oi_change = d.get("oi_change_pct")
        oi_change_text = "нет данных" if oi_change is None else f"{float(oi_change):+.2f}%"
        oi_value = d.get("open_interest_value")
        oi_value_text = "нет данных" if oi_value is None else f"{float(oi_value):,.0f}".replace(",", " ")
        await message.answer(
            f"🧨 FUNDING / OI — {coin}\n\n"
            f"Perp: {d.get('symbol','?')}\n"
            f"Funding: {funding_text}\n"
            f"Статус funding: {d.get('funding_state','UNKNOWN')}\n"
            f"Комментарий: {d.get('funding_comment','нет данных')}\n\n"
            f"OI state: {d.get('oi_state','UNKNOWN')}\n"
            f"OI value: {oi_value_text}\n"
            f"OI change: {oi_change_text}\n"
            f"Комментарий: {d.get('oi_comment','нет данных')}"
        )
    except Exception as exc:
        await message.answer(f"Не смог получить funding/OI {coin}: {exc}")


async def cmd_news(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /news ETH")
        return
    coin = normalize_coin(parts[1])
    try:
        item = await asyncio.to_thread(analyze_coin, make_exchange(), coin)
        n = item.get("news_risk") or {}
        lines = [
            f"📰 NEWS RISK — {coin}",
            "",
            f"Статус: {n.get('state','UNKNOWN')} ({n.get('risk_score',0)}/100)",
            f"Комментарий: {n.get('comment','нет данных')}",
        ]
        if n.get('hits'):
            lines.append(f"Ключевые слова риска: {', '.join(n.get('hits', [])[:8])}")
        items = n.get('items') or []
        if items:
            lines.append("\nСвежие найденные новости:")
            for row in items[:3]:
                title = row.get('title','')
                if len(title) > 100:
                    title = title[:97] + '…'
                lines.append(f"• {title}")
        else:
            lines.append("\nСвежих новостей по монете в RSS не найдено.")
        await message.answer("\n".join(lines))
    except Exception as exc:
        await message.answer(f"Не смог получить новости {coin}: {exc}")


async def cmd_review(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        positions = get_open_positions()
        if not positions:
            await message.answer("🧠 Открытых позиций для review пока нет.")
            return
        items = await asyncio.to_thread(analyze_market)
        exchange = make_exchange()
        market_context = await asyncio.to_thread(build_market_context, items, exchange, fetch_ohlcv_df)
        btc = next((x for x in items if x.get("coin") == "BTC"), None)
        item_map = {x.get("coin"): x for x in items if "error" not in x}
        lines = ["🧠 REVIEW ПОЗИЦИЙ\n"]
        for coin, pos in positions.items():
            item = item_map.get(coin)
            sig = classify_signal(item, btc, market_context) if item else {}
            q = position_quality_score(coin, pos, item, sig, market_context)
            lines.append(format_position_quality(q))
            lines.append("")
        review_text = "\n".join(lines)
        try:
            add_system_event("REVIEW", "Review открытых позиций", text=review_text)
        except Exception:
            pass
        for chunk in split_text(review_text):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка review: {exc}")


async def cmd_backtest(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    coin = normalize_coin(parts[1]) if len(parts) >= 2 else "ETH"
    start = parts[2] if len(parts) >= 3 else DEFAULT_START
    end = parts[3] if len(parts) >= 4 else DEFAULT_END
    try:
        await message.answer(f"🧪 Запускаю бэктест {coin} за {start} — {end}...")
        result = await asyncio.to_thread(backtest_coin, coin, start, end)
        report = format_backtest_coin_report(result)
        try:
            add_system_event("BACKTEST", f"Backtest {coin}", data={"coin": coin, "start": start, "end": end}, text=report)
        except Exception:
            pass
        for chunk in split_text(report):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка backtest {coin}: {exc}\n\nФормат: /backtest ETH или /backtest ETH 2026-04-01 2026-05-31")


async def cmd_backtestall(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    start = parts[1] if len(parts) >= 2 else DEFAULT_START
    end = parts[2] if len(parts) >= 3 else DEFAULT_END
    try:
        await message.answer(f"🧪 Запускаю бэктест всех монет за {start} — {end}. Это может занять 1–3 минуты...")
        results = await asyncio.to_thread(backtest_all, start, end)
        report = format_backtest_all_report(results)
        try:
            add_system_event("BACKTEST_ALL", "Backtest всех монет", data={"start": start, "end": end}, text=report)
        except Exception:
            pass
        for chunk in split_text(report):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка backtest all: {exc}\n\nФормат: /backtestall или /backtestall 2026-04-01 2026-05-31")



async def btn_note(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🗒️ Заметка для анализа\n\n"
        "Напиши так:\n"
        "/note ETH дал слишком ранний сигнал, но funding был перегрет\n\n"
        "Потом ты сможешь выгрузить все заметки и события через /exportlog."
    )

async def btn_backtest(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🧪 Backtest Engine\n\n"
        "Команды:\n"
        "/backtest ETH — проверить одну монету за апрель-май 2026\n"
        "/backtest SOL 2026-04-01 2026-05-31 — свой период\n"
        "/backtestall — проверить все монеты\n\n"
        "Логика теста:\n"
        "• вход от просадки к 30d high;\n"
        "• доборы от последней фактической покупки;\n"
        "• выход на первом TP от средней;\n"
        "• расчет по дневным close-свечам Bybit."
    )



async def cmd_lab(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    # Форматы:
    # /lab — все монеты за последние 5 лет
    # /lab 5y — все монеты за последние 5 лет
    # /lab ETH 5y — одна монета за последние 5 лет
    # /lab 2024-01-01 2026-05-30 — все монеты
    # /lab ETH 2024-01-01 2026-05-30 — одна монета
    coin = None
    end = DEFAULT_END
    start = five_year_start(end)
    if len(parts) == 2:
        arg = parts[1].lower()
        if arg in {"5y", "5years", "5лет"}:
            start = five_year_start(end)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            start = parts[1]
        else:
            coin = normalize_coin(parts[1])
    elif len(parts) >= 3:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            start = parts[1]
            end = parts[2]
        else:
            coin = normalize_coin(parts[1])
            if parts[2].lower() in {"5y", "5years", "5лет"}:
                start = five_year_start(end)
            else:
                start = parts[2]
                end = parts[3] if len(parts) >= 4 else DEFAULT_END
    try:
        target = coin if coin else "всем монетам"
        await message.answer(
            f"🧪 Запускаю Strategy Lab по {target} за {start} — {end}.\n"
            "Бот переберет варианты DCA/TP, выберет лучшие и сохранит их в логику сигналов. Это может занять несколько минут."
        )
        summary = await asyncio.to_thread(run_strategy_lab, start, end, coin, True)
        report = format_strategy_lab_report(summary)
        try:
            add_system_event("STRATEGY_LAB", "Оптимизация стратегии", data={"start": start, "end": end, "coin": coin}, text=report)
        except Exception:
            pass
        for chunk in split_text(report):
            await message.answer(chunk)
        path = summary.get("report_path")
        if path:
            await message.answer_document(FSInputFile(path), caption="🧪 Strategy Lab CSV")
    except Exception as exc:
        log_error("cmd_lab", exc, {"text": message.text})
        await message.answer(
            f"Ошибка Strategy Lab: {exc}\n\n"
            "Форматы:\n"
            "/lab\n"
            "/lab ETH\n"
            "/lab 2024-01-01 2026-05-30\n"
            "/lab ETH 2024-01-01 2026-05-30"
        )


async def cmd_entrylevels(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    coin = normalize_coin(parts[1]) if len(parts) >= 2 else None
    try:
        target = coin if coin else "всем монетам"
        await message.answer(f"📐 Считаю уровни входа по {target}. Это займёт ~1 минуту...")
        summary = await asyncio.to_thread(run_entry_levels, [coin] if coin else None)
        report = format_entry_levels_report(summary)
        for chunk in split_text(report):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка расчёта entry levels: {exc}")


async def cmd_strategyparams(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(format_strategy_params())

async def cmd_robust(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    coin = None
    end = DEFAULT_END
    start = five_year_start(end)
    # Форматы:
    # /robust — все монеты за 5 лет
    # /robust 5y — все монеты за 5 лет
    # /robust ETH 5y — одна монета за 5 лет
    # /robust 2022-01-01 2026-05-30 — все монеты за период
    # /robust ETH 2022-01-01 2026-05-30 — одна монета за период
    if len(parts) == 2:
        arg = parts[1].lower()
        if arg in {"5y", "5years", "5лет"}:
            start = five_year_start(end)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            start = parts[1]
        else:
            coin = normalize_coin(parts[1])
    elif len(parts) >= 3:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            start = parts[1]
            end = parts[2]
        else:
            coin = normalize_coin(parts[1])
            if parts[2].lower() in {"5y", "5years", "5лет"}:
                start = five_year_start(end)
            else:
                start = parts[2]
                end = parts[3] if len(parts) >= 4 else DEFAULT_END
    try:
        target = coin if coin else "всем монетам"
        await message.answer(
            f"🧬 Запускаю Robustness Lab по {target} за {start} — {end}.\n"
            "Это глубже обычного /lab: бот проверит устойчивость по годам, walk-forward и штраф за переобучение. Может занять несколько минут."
        )
        summary = await asyncio.to_thread(run_robustness_lab, start, end, coin, True)
        report = format_robustness_report(summary)
        try:
            add_system_event("ROBUSTNESS_LAB", "Проверка устойчивости стратегии", data={"start": start, "end": end, "coin": coin}, text=report)
        except Exception:
            pass
        for chunk in split_text(report):
            await message.answer(chunk)
        path = summary.get("report_path")
        if path:
            await message.answer_document(FSInputFile(path), caption="🧬 Robustness Lab CSV")
    except Exception as exc:
        log_error("cmd_robust", exc, {"text": message.text})
        await message.answer(
            f"Ошибка Robustness Lab: {exc}\n\n"
            "Форматы:\n"
            "/robust\n"
            "/robust ETH\n"
            "/robust 2022-01-01 2026-05-30\n"
            "/robust ETH 2022-01-01 2026-05-30"
        )


async def cmd_reliability(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(format_reliability_report())


async def btn_robust(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🧬 Robustness Lab\n\n"
        "Это проверка, чтобы не подгонять стратегию под прошлое.\n\n"
        "Команды:\n"
        "/robust — проверить все монеты за 5 лет\n"
        "/robust ETH — проверить одну монету\n"
        "/robust 2022-01-01 2026-05-30 — свой период\n"
        "/reliability — рейтинг надежности монет\n\n"
        "После /robust бот сохраняет устойчивые DCA/TP и рейтинг надежности."
    )


async def btn_lab(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🧪 Strategy Lab\n\n"
        "Это не отдельная программа, а лаборатория внутри бота. Она берет историю Bybit, перебирает DCA/TP и сохраняет лучшие параметры в data/strategy_params.json.\n\n"
        "Команды:\n"
        "/lab — оптимизировать все монеты за 5 лет\n"
        "/lab ETH — оптимизировать одну монету\n"
        "/lab 2024-01-01 2026-05-30 — свой период\n"
        "/strategyparams — посмотреть активные параметры\n\n"
        "После /lab эти параметры используются в /scan, DCA и TP-подсказках."
    )


async def cmd_stats(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        price_map = await build_price_map()
        await message.answer(format_stats_report(price_map))
    except Exception as exc:
        await message.answer(f"Ошибка статистики: {exc}")


async def cmd_export(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        path = export_journal_csv()
        await message.answer_document(FSInputFile(path), caption="📤 Журнал сделок CSV")
    except Exception as exc:
        await message.answer(f"Не смог выгрузить CSV: {exc}")



async def cmd_note(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    text = (message.text or "").split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await message.answer("Формат: /note ETH дал ранний сигнал, нужно повысить порог score")
        return
    add_user_note(text[1].strip())
    await message.answer("🗒️ Заметка сохранена в журнал контекста.")


async def cmd_logsummary(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(build_context_summary())


async def cmd_exportlog(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        path = export_context_markdown()
        await message.answer_document(FSInputFile(path), caption="📦 Журнал контекста для ChatGPT")
    except Exception as exc:
        await message.answer(f"Не смог выгрузить лог: {exc}")


async def cmd_errors(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(format_error_summary())


async def cmd_exporterrors(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        path = export_errors_markdown()
        await message.answer_document(FSInputFile(path), caption="⚠️ Журнал ошибок для ChatGPT")
    except Exception as exc:
        await message.answer(f"Не смог выгрузить ошибки: {exc}")


async def cmd_mldataset(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    parts = (message.text or "").split()
    # Форматы:
    # /mldataset — все монеты за период по умолчанию
    # /mldataset 2026-04-01 2026-05-31 — все монеты за период
    # /mldataset ETH 2026-04-01 2026-05-31 — одна монета
    coin = None
    start = DEFAULT_START
    end = DEFAULT_END
    if len(parts) == 2:
        arg = parts[1].lower()
        if arg in {"5y", "5years", "5лет"}:
            start = five_year_start(end)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            start = parts[1]
        else:
            coin = normalize_coin(parts[1])
    elif len(parts) >= 3:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            start = parts[1]
            end = parts[2]
        else:
            coin = normalize_coin(parts[1])
            if parts[2].lower() in {"5y", "5years", "5лет"}:
                start = five_year_start(end)
            else:
                start = parts[2]
                end = parts[3] if len(parts) >= 4 else DEFAULT_END
    try:
        target = coin if coin else "всем монетам"
        await message.answer(f"🤖 Собираю ML-датасет по {target} за {start} — {end}. Это может занять 1–3 минуты...")
        path, summary = await asyncio.to_thread(export_ml_dataset, start, end, coin)
        add_system_event("ML_DATASET", "Сбор ML-датасета", data=summary)
        await message.answer(format_ml_dataset_summary(summary))
        await message.answer_document(FSInputFile(path), caption="🤖 ML dataset CSV")
    except Exception as exc:
        log_error("cmd_mldataset", exc, {"text": message.text})
        await message.answer(f"Ошибка ML Dataset: {exc}\n\nФорматы:\n/mldataset\n/mldataset ETH\n/mldataset 2026-04-01 2026-05-31\n/mldataset ETH 2026-04-01 2026-05-31")


async def btn_mldataset(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🤖 ML Dataset Builder\n\n"
        "Команды:\n"
        "/mldataset — собрать датасет по всем монетам за апрель-май 2026\n"
        "/mldataset ETH — только ETH\n"
        "/mldataset 2026-04-01 2026-05-31 — свой период\n\n"
        "Бот соберет CSV: признаки сигнала + результат через 14/30 дней. Потом на этом можно обучать модель."
    )


async def btn_errors(message: Message):
    await cmd_errors(message)


async def on_error(event: ErrorEvent):
    try:
        update = getattr(event, "update", None)
        ctx = {}
        if update is not None:
            ctx["update_id"] = getattr(update, "update_id", None)
            ctx["event_type"] = getattr(update, "event_type", None)
        log_error("aiogram_unhandled", event.exception, ctx)
    except Exception:
        pass
    return True

async def cmd_monitor(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return

    last_scan = "еще не было"
    if STATE_PATH.exists():
        try:
            import json
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            last_scan = state.get("_last_scan", last_scan)
        except Exception:
            pass

    await message.answer(
        "🤖 Автомониторинг v14\n"
        f"Рынок: {'включен' if settings.auto_monitor_enabled else 'выключен'} / {settings.scan_interval_minutes} мин.\n"
        f"Bybit portfolio: {'включен' if settings.bybit_portfolio_monitor_enabled else 'выключен'} / {settings.bybit_portfolio_monitor_minutes} мин.\n"
        f"Последний рыночный скан: {last_scan}\n\n"
        "Бот следит за:\n"
        "• новыми покупками/продажами на Bybit;\n"
        "• открытыми позициями;\n"
        "• добором по адаптивной лесенке;\n"
        "• фиксацией по TP уровням."
    )


async def btn_scan(message: Message):
    await cmd_scan(message)


async def btn_status(message: Message):
    await cmd_status(message)


async def btn_positions(message: Message):
    await cmd_positions(message)


async def btn_journal(message: Message):
    await cmd_journal(message)


async def btn_stats(message: Message):
    await cmd_stats(message)


async def btn_risk(message: Message):
    await cmd_risk(message)


async def btn_export(message: Message):
    await cmd_export(message)


async def btn_funding(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer("Введи команду: /funding ETH\nПримеры: /funding BTC, /funding SOL")


async def btn_news(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer("Введи команду: /news ETH\nПримеры: /news BTC, /news SOL")


async def btn_monitor(message: Message):
    await cmd_monitor(message)


async def btn_help(message: Message):
    await cmd_help(message)


async def btn_price(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "Введи команду цены в формате:\n\n"
        "/price ETH\n\n"
        "Примеры: /price BTC, /price SOL, /price LINK"
    )


async def btn_buy(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "Запиши покупку в формате:\n\n"
        "/buy ETH 10 2178.5\n\n"
        "Где:\n"
        "• ETH — монета\n"
        "• 10 — сумма покупки в USDT\n"
        "• 2178.5 — фактическая цена покупки на Bybit\n\n"
        "Важно: следующий добор бот считает от твоей последней фактической цены покупки."
    )


async def btn_sell(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "Запиши продажу в формате:\n\n"
        "/sell ETH all — продать всю позицию по текущей цене\n"
        "/sell ETH all 2300 — продать всю позицию по твоей цене\n"
        "/sell ETH 5 2300 — продать на 5 USDT по твоей цене"
    )


async def on_callback(call: CallbackQuery):
    try:
        await call.answer()
    except TelegramBadRequest:
        pass

    if not is_allowed(call):
        await call.message.answer("Нет доступа")
        return

    data = call.data or ""

    if data.startswith("buy_now:"):
        parts = data.split(":")
        if len(parts) >= 3:
            coin = parts[1]
            try:
                entry_usdt = float(parts[2])
            except ValueError:
                entry_usdt = 0.0
            try:
                price = await get_current_price(coin)
                allowed, risk_msg, _risk_check = await asyncio.to_thread(check_buy_allowed, coin, entry_usdt, price)
                if not allowed:
                    await call.message.answer(risk_msg)
                    return
                pos = record_buy(coin, entry_usdt, price, reason="signal_button")
                # Spot journal: log signal-button buy
                try:
                    _market_item = await asyncio.to_thread(analyze_coin, make_exchange(), coin)
                    _deriv = (_market_item.get("derivatives") or {})
                    _news = (_market_item.get("news_risk") or {})
                    _dd90 = _market_item.get("drawdown_90d_high")
                    _levels = get_entry_levels(coin)
                    _trigger, _trigger_pct = "сигнал", None
                    if _levels and _dd90 is not None:
                        _dd_abs = abs(float(_dd90))
                        if _levels.get("t3") and _dd_abs >= float(_levels["t3"]):
                            _trigger, _trigger_pct = f"t3 (-{_levels['t3']}%)", float(_levels["t3"])
                        elif _levels.get("t2") and _dd_abs >= float(_levels["t2"]):
                            _trigger, _trigger_pct = f"t2 (-{_levels['t2']}%)", float(_levels["t2"])
                        elif _levels.get("t1") and _dd_abs >= float(_levels["t1"]):
                            _trigger, _trigger_pct = f"t1 (-{_levels['t1']}%)", float(_levels["t1"])
                    log_spot_buy(
                        symbol=coin,
                        tranche=int(pos.get("entry_count", 1)),
                        entry_price=price,
                        amount_usdt=entry_usdt,
                        qty=entry_usdt / price if price else 0,
                        trigger=_trigger,
                        trigger_pct=_trigger_pct,
                        auto=True,
                        indicators={
                            "drawdown_pct": _dd90,
                            "score": None,
                            "volume_ratio": None,
                            "funding": _deriv.get("funding_pct"),
                            "news_risk": _news.get("state"),
                            "volatility": _market_item.get("volatility_class"),
                            "health": (_risk_check.get("health") or {}).get("status"),
                        },
                    )
                except Exception:
                    pass
                await call.message.answer(
                    f"✅ Записано: {coin} ${entry_usdt:.0f} по ${fmt_usdt(price)}"
                )
            except Exception as exc:
                await call.message.answer(f"Ошибка записи покупки: {exc}")

    elif data == "skip":
        pass

    elif data.startswith("buy:"):
        coin = data.split(":", 1)[1]
        PENDING_BUY[call.from_user.id] = coin
        await call.message.answer(
            f"Введи фактическую покупку для {coin} в формате:\n\n"
            f"10 86.16\n\n"
            f"Где 10 — сумма в USDT, 86.16 — цена, по которой реально купил."
        )

    elif data == "positions":
        price_map = await build_price_map()
        await call.message.answer(format_positions_report(price_map))

    elif data == "journal":
        await call.message.answer(format_journal())

    elif data == "review":
        # callback_query не является Message, поэтому делаем короткий inline-review через существующую команду невозможно.
        await call.message.answer("Напиши /review или нажми кнопку 🧠 Review на клавиатуре.")

    elif data == "monitor":
        await call.message.answer("Напиши /monitor для статуса автомониторинга")

    elif data == "scan":
        await call.message.answer("Сканирую рынок...")
        items = await asyncio.to_thread(cached_analyze_market)
        market_context = await asyncio.to_thread(cached_market_context, items)
        try:
            if settings.save_market_snapshots:
                record_scan_snapshot(items, market_context)
        except Exception:
            pass
        for chunk in split_text(format_signal_report(items, market_context)):
            await call.message.answer(chunk)


async def pending_buy_amount_handler(message: Message):
    if not is_allowed(message):
        return
    user_id = message.from_user.id
    if user_id not in PENDING_BUY:
        return
    text = (message.text or "").strip().replace(",", ".")
    parts = text.split()
    if len(parts) != 2 or not re.fullmatch(r"\d+(\.\d+)?", parts[0]) or not re.fullmatch(r"\d+(\.\d+)?", parts[1]):
        await message.answer(
            "Введи сумму и цену через пробел.\n"
            "Пример: 10 86.16\n\n"
            "10 — сумма в USDT, 86.16 — цена покупки."
        )
        return
    coin = PENDING_BUY.pop(user_id)
    try:
        amount_usdt = float(parts[0])
        price = float(parts[1])
        allowed, risk_msg, risk_check = await asyncio.to_thread(check_buy_allowed, coin, amount_usdt, price)
        if not allowed:
            await message.answer(risk_msg)
            return
        try:
            market_item = await asyncio.to_thread(analyze_coin, make_exchange(), coin)
            dca_levels = market_item.get("dca_levels") or None
        except Exception:
            dca_levels = None
        pos = record_buy(coin, amount_usdt, price, reason="button_signal", dca_levels=dca_levels)
        risk_text = format_buy_risk_warning(risk_check)
        await message.answer(
            f"✅ Покупка записана\n\n"
            f"{coin}: {fmt_money(amount_usdt)} по {fmt_usdt(price)} USDT\n"
            f"Куплено: {amount_usdt / price:.6f} {coin}\n"
            f"Средняя цена: {fmt_usdt(pos['avg_price'])} USDT\n"
            f"Всего вложено: {fmt_money(pos['invested_usdt'])}\n"
            f"Входов: {pos['entry_count']}\n"
            f"Следующий добор: {fmt_usdt(pos['next_buy_price'])} USDT\n\n"
            f"{risk_text}"
        )
    except Exception as exc:
        await message.answer(f"Не смог записать покупку: {exc}")
async def cmd_spotjournal(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(format_spot_journal(10))


async def cmd_spotreport(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        report7 = format_spot_report(7)
        report30 = format_spot_report(30)
        await message.answer(report7)
        await message.answer(report30)
    except Exception as exc:
        await message.answer(f"Ошибка spot report: {exc}")


async def cmd_exportspot(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        path = await asyncio.to_thread(export_spot_csv)
        await message.answer_document(FSInputFile(path), caption="📊 Spot Journal CSV")
    except Exception as exc:
        await message.answer(f"Не смог выгрузить spot journal: {exc}")


async def cmd_futures(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(await asyncio.to_thread(futures_summary))


async def cmd_futuresreport(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        report7 = await asyncio.to_thread(futures_report, 7)
        report30 = await asyncio.to_thread(futures_report, 30)
        for chunk in split_text(report7):
            await message.answer(chunk)
        for chunk in split_text(report30):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка futures report: {exc}")


async def cmd_exportfutures(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        path = await asyncio.to_thread(export_futures_csv, 30)
        await message.answer_document(FSInputFile(path), caption="📊 Futures Lab CSV (30д)")
    except Exception as exc:
        await message.answer(f"Ошибка экспорта futures CSV: {exc}")


async def cmd_circuitstatus(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        text = await asyncio.to_thread(circuit_status)
        for chunk in split_text(text):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка circuit status: {exc}")


async def cmd_rotation(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(await asyncio.to_thread(rotation_summary))


async def cmd_rotation_history(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(await asyncio.to_thread(rotation_history))


async def cmd_rotation_tick(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    res = await asyncio.to_thread(rotation_tick)
    await message.answer(f"🧪 Rotation tick\n\n{res}")


async def cmd_rotation_reset(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(await asyncio.to_thread(rotation_reset))


async def cmd_rotationreport(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        report7 = await asyncio.to_thread(rotation_report, 7)
        report30 = await asyncio.to_thread(rotation_report, 30)
        for chunk in split_text(report7):
            await message.answer(chunk)
        for chunk in split_text(report30):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка rotation report: {exc}")


async def cmd_exportrotation(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        path = await asyncio.to_thread(export_rotation_csv, 30)
        await message.answer_document(FSInputFile(path), caption="📊 Rotation Lab CSV (30д)")
    except Exception as exc:
        await message.answer(f"Ошибка экспорта rotation CSV: {exc}")


# ─── PRO Lab ─────────────────────────────────────────────────────────────────
async def cmd_pro(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(await asyncio.to_thread(pro_summary))


async def cmd_proreport(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        report7 = await asyncio.to_thread(pro_report, 7)
        report30 = await asyncio.to_thread(pro_report, 30)
        for chunk in split_text(report7):
            await message.answer(chunk)
        for chunk in split_text(report30):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка pro report: {exc}")


async def cmd_exportpro(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        path = await asyncio.to_thread(export_pro_csv, 30)
        await message.answer_document(FSInputFile(path), caption="📊 PRO Lab CSV (30д)")
    except Exception as exc:
        await message.answer(f"Ошибка экспорта PRO CSV: {exc}")


async def cmd_battle(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        text = await asyncio.to_thread(battle_report)
        for chunk in split_text(text):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка battle: {exc}")


# ─── Unified active-trades dashboard (spot + futures + pro + rotation) ──────────
def _symbol_base(symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC'."""
    return (symbol or "").split("/")[0]


def _directional_pnl_pct(side: str, entry: float, current: float | None) -> float | None:
    if not current or not entry or entry <= 0:
        return None
    return (current / entry - 1) * 100 if side == "long" else (entry / current - 1) * 100


def build_active_trades_report(price_map: dict[str, float] | None = None) -> str:
    """Single dashboard of every open position across all four systems.

    ``price_map`` maps coin (e.g. 'BTC') -> USDT spot price. Perp positions use the
    base coin's price; rotation pairs ('X/BTC') are derived as price[X]/price[BTC].
    Missing prices degrade gracefully to entry-only lines.
    """
    price_map = price_map or {}
    lines = ["📊 АКТИВНЫЕ СДЕЛКИ", ""]
    total = 0

    # 💰 SPOT — real portfolio
    lines.append("💰 СПОТ (реальный портфель):")
    positions = get_open_positions()
    if positions:
        for coin, pos in positions.items():
            avg = float(pos.get("avg_price", 0) or 0)
            qty = float(pos.get("qty", 0) or 0)
            entry_count = int(pos.get("entry_count", 1) or 1)
            dca = pos.get("dca_levels_used") or []
            max_tr = max(len(dca) + 1, 3)
            cur = price_map.get(coin)
            pnl = _directional_pnl_pct("long", avg, cur)
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "н/д"
            cur_str = f"${fmt_usdt(cur)}" if cur else "—"
            lines.append(
                f"  • {coin}: вход ${fmt_usdt(avg)} | сейчас {cur_str} | "
                f"PnL {pnl_str} | qty {qty:.6f} | транш {entry_count}/{max_tr}"
            )
            total += 1
    else:
        lines.append("  нет открытых позиций")
    lines.append("")

    # 🤖 FUTURES — our bot, demo
    lines.append("🤖 ФЬЮЧЕРСЫ (наш бот, демо):")
    fa = (load_futures_state() or {}).get("active_trade")
    if fa:
        side = str(fa.get("side", "")).lower()
        entry = float(fa.get("entry_price") or 0)
        stop = float(fa.get("trailing_stop") or fa.get("stop_price") or 0)
        cur = price_map.get(_symbol_base(fa.get("symbol", "")))
        pnl = _directional_pnl_pct(side, entry, cur)
        pnl_str = f"{pnl:+.1f}%" if pnl is not None else "н/д"
        cur_str = f"{cur:g}" if cur else "—"
        score = fa.get("score")
        score_str = f" | score {score}" if score is not None else ""
        lines.append(
            f"  • {fa.get('symbol')} {side.upper()} [{fa.get('strategy')}]{score_str}\n"
            f"    вход {entry:g} | сейчас {cur_str} | PnL {pnl_str} | стоп {stop:g}"
        )
        total += 1
    else:
        lines.append("  нет активной сделки")
    lines.append("")

    # 🎯 PRO FUTURES — demo
    lines.append("🎯 ФЬЮЧЕРСЫ ПРО (демо):")
    pa = (load_pro_state() or {}).get("active_trade")
    if pa:
        side = str(pa.get("side", "")).lower()
        entry = float(pa.get("avg_entry_price") or pa.get("entry_price") or 0)
        stop = float(pa.get("trailing_stop") or pa.get("stop_price") or 0)
        cur = price_map.get(_symbol_base(pa.get("symbol", "")))
        pnl = _directional_pnl_pct(side, entry, cur)
        pnl_str = f"{pnl:+.1f}%" if pnl is not None else "н/д"
        cur_str = f"{cur:g}" if cur else "—"
        adds = int(pa.get("adds") or 0)
        lines.append(
            f"  • {pa.get('symbol')} {side.upper()} | доборов {adds}\n"
            f"    вход {entry:g} | сейчас {cur_str} | PnL {pnl_str} | стоп {stop:g}"
        )
        total += 1
    else:
        lines.append("  нет активной сделки")
    lines.append("")

    # 🔄 ROTATION — crypto pairs, demo
    lines.append("🔄 КРИПТОПАРЫ (ротация, демо):")
    ra = (load_rotation_state() or {}).get("active_trade")
    if ra:
        pair = ra.get("pair", "")
        entry = float(ra.get("entry_price") or 0)
        parts = pair.split("/")
        cur = None
        if len(parts) == 2:
            base_p = price_map.get(parts[0])
            quote_p = price_map.get(parts[1])
            if base_p and quote_p:
                cur = base_p / quote_p
        pnl = _directional_pnl_pct("long", entry, cur)
        pnl_str = f"{pnl:+.1f}%" if pnl is not None else "н/д"
        cur_str = f"{cur:.8g}" if cur else "—"
        trailing = "активен" if ra.get("trailing_activated") else "ждёт"
        lines.append(
            f"  • {pair}: вход {entry:.8g} | сейчас {cur_str} | "
            f"PnL {pnl_str} | трейлинг {trailing}"
        )
        total += 1
    else:
        lines.append("  нет активной сделки")
    lines.append("")

    lines.append("━━━━━━━━━━━━")
    lines.append(f"Всего открытых позиций по всем системам: {total}")
    return "\n".join(lines)


async def cmd_active(message: Message):
    if not is_allowed(message):
        await message.answer("Нет доступа.")
        return
    try:
        price_map = await build_price_map()
        # Augment the map with any open perp/rotation instruments not in our COINS map.
        needed: set[str] = set()
        fa = (await asyncio.to_thread(load_futures_state) or {}).get("active_trade")
        if fa:
            needed.add(_symbol_base(fa.get("symbol", "")))
        pa = (await asyncio.to_thread(load_pro_state) or {}).get("active_trade")
        if pa:
            needed.add(_symbol_base(pa.get("symbol", "")))
        ra = (await asyncio.to_thread(load_rotation_state) or {}).get("active_trade")
        if ra:
            needed.update(ra.get("pair", "").split("/"))
        for coin in needed:
            if coin and coin not in price_map:
                try:
                    price_map[coin] = await get_current_price(coin)
                except Exception:
                    pass
        text = await asyncio.to_thread(build_active_trades_report, price_map)
        for chunk in split_text(text):
            await message.answer(chunk)
    except Exception as exc:
        await message.answer(f"Ошибка дашборда активных сделок: {exc}")


async def main():
    if not settings.telegram_bot_token:
        raise RuntimeError("Не указан TELEGRAM_BOT_TOKEN в .env")

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()

    # Команды
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_portfolio, Command("portfolio"))
    dp.message.register(cmd_portfolio, Command("plan"))
    dp.message.register(cmd_positions, Command("positions"))

    dp.message.register(cmd_bybit, Command("bybit"))
    dp.message.register(cmd_syncbybit, Command("syncbybit"))
    dp.message.register(cmd_bybitcheck, Command("bybitcheck"))

    dp.message.register(cmd_review, Command("review"))
    dp.message.register(cmd_risk, Command("risk"))

    dp.message.register(cmd_scan, Command("scan"))
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(cmd_changes, Command("changes"))
    dp.message.register(cmd_memory, Command("memory"))

    dp.message.register(cmd_price, Command("price"))
    dp.message.register(cmd_funding, Command("funding"))
    dp.message.register(cmd_news, Command("news"))

    dp.message.register(cmd_backtest, Command("backtest"))
    dp.message.register(cmd_backtestall, Command("backtestall"))

    dp.message.register(cmd_lab, Command("lab"))
    dp.message.register(cmd_entrylevels, Command("entrylevels"))

    dp.message.register(cmd_spotjournal, Command("spotjournal"))
    dp.message.register(cmd_spotreport, Command("spotreport"))
    dp.message.register(cmd_exportspot, Command("exportspot"))

    dp.message.register(cmd_strategyparams, Command("strategyparams"))
    dp.message.register(cmd_robust, Command("robust"))
    dp.message.register(cmd_reliability, Command("reliability"))

    dp.message.register(cmd_note, Command("note"))

    dp.message.register(cmd_logsummary, Command("logsummary"))
    dp.message.register(cmd_exportlog, Command("exportlog"))

    dp.message.register(cmd_errors, Command("errors"))
    dp.message.register(cmd_exporterrors, Command("exporterrors"))

    dp.message.register(cmd_mldataset, Command("mldataset"))
    dp.message.register(cmd_mldataset, Command("mlsummary"))

    dp.message.register(cmd_buy, Command("buy"))
    dp.message.register(cmd_sell, Command("sell"))

    dp.message.register(cmd_journal, Command("journal"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_export, Command("export"))

    dp.message.register(cmd_monitor, Command("monitor"))

    # Futures Lab
    dp.message.register(cmd_futures, Command("futures"))
    dp.message.register(cmd_futuresreport, Command("futuresreport"))
    dp.message.register(cmd_exportfutures, Command("exportfutures"))
    dp.message.register(cmd_circuitstatus, Command("circuitstatus"))

    # Rotation Lab
    dp.message.register(cmd_rotation, Command("rotation"))
    dp.message.register(cmd_rotation_history, Command("rotationhistory"))
    dp.message.register(cmd_rotation_tick, Command("rotationtick"))
    dp.message.register(cmd_rotation_reset, Command("rotationreset"))
    dp.message.register(cmd_rotationreport, Command("rotationreport"))
    dp.message.register(cmd_exportrotation, Command("exportrotation"))

    # PRO Lab + battle + unified active dashboard
    dp.message.register(cmd_pro, Command("pro"))
    dp.message.register(cmd_proreport, Command("proreport"))
    dp.message.register(cmd_exportpro, Command("exportpro"))
    dp.message.register(cmd_battle, Command("battle"))
    dp.message.register(cmd_active, Command("active"))

    # Кнопки
    dp.message.register(btn_scan, F.text.in_({"📊 Скан", "🔍 Скан"}))
    dp.message.register(btn_status, F.text.in_({"🌍 Статус", "🌍 Рынок"}))

    dp.message.register(cmd_changes, F.text == "📈 Изменения")
    dp.message.register(cmd_memory, F.text == "🧠 Память")

    dp.message.register(btn_positions, F.text == "💼 Позиции")
    dp.message.register(cmd_active, F.text == "📊 Активные сделки")

    dp.message.register(cmd_bybit, F.text == "🔗 Bybit")
    dp.message.register(cmd_syncbybit, F.text == "🔄 Sync Bybit")

    dp.message.register(
        cmd_bybitcheck,
        F.text.in_({"👁️ Bybit Check", "👁️ Проверить Bybit"}),
    )

    dp.message.register(cmd_review, F.text == "🧠 Review")

    dp.message.register(btn_lab, F.text == "🧪 Lab")
    dp.message.register(btn_robust, F.text == "🧬 Robust")

    dp.message.register(cmd_reliability, F.text == "📈 Reliability")

    dp.message.register(btn_backtest, F.text == "🧪 Backtest")

    dp.message.register(btn_mldataset, F.text == "🤖 ML Dataset")

    dp.message.register(cmd_strategyparams, F.text == "⚙️ Параметры")

    dp.message.register(btn_errors, F.text == "⚠️ Ошибки")

    dp.message.register(btn_journal, F.text == "📒 Журнал")

    dp.message.register(btn_stats, F.text == "📊 Статистика")

    dp.message.register(btn_risk, F.text == "🛡️ Риски")

    dp.message.register(btn_export, F.text == "📤 CSV")

    dp.message.register(btn_funding, F.text == "🧨 Funding/OI")

    dp.message.register(btn_news, F.text == "📰 Новости")

    dp.message.register(btn_price, F.text == "💵 Цена")

    dp.message.register(btn_buy, F.text == "➕ Покупка")

    dp.message.register(btn_sell, F.text == "➖ Продажа")

    dp.message.register(btn_monitor, F.text == "🤖 Монитор")

    dp.message.register(btn_help, F.text == "❓ Помощь")

    # Rotation кнопки
    dp.message.register(
        cmd_rotation,
        F.text == "🧪 Демо торговля",
    )

    dp.message.register(
        cmd_rotation_history,
        F.text == "📜 Демо журнал",
    )

    dp.errors.register(on_error)
    dp.callback_query.register(on_callback)

    dp.message.register(
        pending_buy_amount_handler,
        F.text,
    )

    if settings.auto_monitor_enabled:
        asyncio.create_task(
            monitor_loop(bot)
        )

    asyncio.create_task(
        auto_strategy_bootstrap(bot)
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
