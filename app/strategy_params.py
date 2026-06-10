from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import CATEGORIES, BASE_DCA_LEVELS, TAKE_PROFIT_LEVELS, normalize_coin

DATA_DIR = Path('data')
PARAMS_PATH = DATA_DIR / 'strategy_params.json'


def _default_for_coin(coin: str) -> dict[str, Any]:
    coin = normalize_coin(coin)
    cat = CATEGORIES.get(coin, 'STRONG_ALT')
    return {
        'coin': coin,
        'category': cat,
        'dca_levels': list(BASE_DCA_LEVELS.get(cat, BASE_DCA_LEVELS['STRONG_ALT'])),
        'tp_levels': list(TAKE_PROFIT_LEVELS.get(cat, TAKE_PROFIT_LEVELS['STRONG_ALT'])),
        'source': 'default_config',
    }


def load_strategy_params() -> dict[str, Any]:
    if not PARAMS_PATH.exists():
        return {'version': 1, 'coins': {}}
    try:
        data = json.loads(PARAMS_PATH.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {'version': 1, 'coins': {}}
        data.setdefault('version', 1)
        data.setdefault('coins', {})
        return data
    except Exception:
        return {'version': 1, 'coins': {}}


def save_strategy_params(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    PARAMS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def update_coin_params(coin: str, dca_levels: list[float], tp_levels: list[float], meta: dict[str, Any] | None = None) -> None:
    coin = normalize_coin(coin)
    data = load_strategy_params()
    data.setdefault('coins', {})[coin] = {
        **_default_for_coin(coin),
        'dca_levels': [float(x) for x in dca_levels[:4]],
        'tp_levels': [float(x) for x in tp_levels[:3]],
        'source': 'strategy_lab',
        'meta': meta or {},
    }
    save_strategy_params(data)


def get_coin_params(coin: str) -> dict[str, Any]:
    coin = normalize_coin(coin)
    data = load_strategy_params()
    saved = (data.get('coins') or {}).get(coin)
    default = _default_for_coin(coin)
    if not saved:
        return default
    # Безопасный merge, чтобы не падать от старого файла.
    result = {**default, **saved}
    result['dca_levels'] = [float(x) for x in result.get('dca_levels') or default['dca_levels']]
    result['tp_levels'] = [float(x) for x in result.get('tp_levels') or default['tp_levels']]
    return result


def get_dca_levels(coin: str) -> list[float]:
    return list(get_coin_params(coin)['dca_levels'])


def get_tp_levels(coin: str) -> list[float]:
    return list(get_coin_params(coin)['tp_levels'])


def format_strategy_params() -> str:
    data = load_strategy_params()
    coins = data.get('coins') or {}
    if not coins:
        return (
            '🧪 STRATEGY PARAMS\n\n'
            'Оптимизированных параметров пока нет.\n'
            'Запусти /lab 2024-01-01 2026-05-30, чтобы бот подобрал лесенки и TP по истории.'
        )
    lines = ['🧪 STRATEGY PARAMS', '', 'Активные параметры из Strategy Lab:']
    for coin in sorted(coins):
        p = get_coin_params(coin)
        dca = ' / '.join(f'{x:.0f}%' for x in p['dca_levels'])
        tp = ' / '.join(f'+{x:.0f}%' for x in p['tp_levels'])
        meta = p.get('meta') or {}
        score = meta.get('score') or meta.get('robust_score')
        reliability = meta.get('reliability')
        pnl = meta.get('total_pnl_usdt')
        suffix = ''
        if reliability is not None:
            suffix += f' | reliability {float(reliability):.0f}/100'
        if score is not None:
            suffix += f' | score {float(score):.2f}'
        if pnl is not None:
            suffix += f' | pnl {float(pnl):+.2f} USDT'
        lines.append(f'• {coin}: DCA {dca} | TP {tp}{suffix}')
    return '\n'.join(lines)
