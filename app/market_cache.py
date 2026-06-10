"""Simple in-process cache for heavy market requests.

This does not replace persistent market memory. It only prevents the bot from
hitting Bybit/RSS/Funding endpoints repeatedly when the user presses buttons
several times in a row.
"""
from __future__ import annotations

import time
from typing import Any, Callable

_cache: dict[str, tuple[float, Any]] = {}


def get_cached(key: str, ttl_seconds: int, factory: Callable[[], Any], force: bool = False) -> Any:
    now = time.time()
    if not force and key in _cache:
        ts, value = _cache[key]
        if now - ts <= ttl_seconds:
            return value
    value = factory()
    _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    _cache.clear()
