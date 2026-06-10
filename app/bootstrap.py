from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from app.settings import settings
from app.strategy_robustness import run_robustness_lab, format_robustness_report, RELIABILITY_PATH
from app.strategy_params import PARAMS_PATH, format_strategy_params
from app.ui import split_text
from app.error_log import log_error


def _file_is_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return datetime.now(timezone.utc) - mtime <= timedelta(days=max_age_days)
    except Exception:
        return False


def strategy_bootstrap_needed(max_age_days: int = 7) -> bool:
    return not (_file_is_fresh(RELIABILITY_PATH, max_age_days) and _file_is_fresh(PARAMS_PATH, max_age_days))


async def auto_strategy_bootstrap(bot) -> None:
    """Автоматическая подготовка стратегии при запуске.

    Если robust-рейтинг и параметры уже свежие, ничего не делаем.
    Если их нет или они старше max_age_days — запускаем /robust 5y в фоне.
    """
    if not settings.auto_strategy_bootstrap_enabled:
        return
    if not strategy_bootstrap_needed(settings.strategy_bootstrap_max_age_days):
        return

    chat_id = settings.telegram_allowed_user_id
    try:
        if chat_id:
            await bot.send_message(
                chat_id,
                "🧬 Автоподготовка стратегии\n\n"
                "Не нашел свежие robust-параметры. Запускаю анализ 5y в фоне. "
                "Это может занять несколько минут. Бот продолжит работать."
            )
        summary = await asyncio.to_thread(run_robustness_lab, None, None, None, True)
        report = format_robustness_report(summary)
        if chat_id:
            for chunk in split_text(report):
                await bot.send_message(chat_id, chunk)
            await bot.send_message(chat_id, format_strategy_params())
    except Exception as exc:
        log_error("auto_strategy_bootstrap", exc, {})
        try:
            if chat_id:
                await bot.send_message(chat_id, f"⚠️ Автоподготовка стратегии не завершилась: {exc}")
        except Exception:
            pass
