from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import csv
import itertools
import json

import pandas as pd

from app.config import COINS, CATEGORIES, BASE_DCA_LEVELS, normalize_coin
from app.backtest import make_public_exchange, fetch_daily_history, _parse_date
from app.strategy_params import update_coin_params, get_coin_params, format_strategy_params

REPORT_DIR = Path('reports')
DATA_DIR = Path('data')
ENTRY_LEVELS_PATH = DATA_DIR / 'entry_levels.json'

ENTRY_USDT = 10.0

DCA_PRESETS = {
    # универсальные пресеты, дальше они масштабируются по категории/монете
    'tight': [0.80, 0.85, 0.90, 0.95],
    'base': [1.00, 1.00, 1.00, 1.00],
    'wide': [1.20, 1.25, 1.30, 1.35],
    'deep': [1.45, 1.55, 1.65, 1.75],
}
TP_CANDIDATES = [10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0]


def five_year_start(end: str | None = None) -> str:
    """Return an approximate 5-year start date for Strategy Lab.

    Bybit may not have full history for newer coins, so the fetcher will use
    whatever candles are actually available for each symbol.
    """
    if end:
        end_dt = _parse_date(end)
    else:
        end_dt = datetime.now(timezone.utc)
    return (end_dt - timedelta(days=365 * 5 + 2)).strftime('%Y-%m-%d')


@dataclass
class SimResult:
    coin: str
    category: str
    preset: str
    dca_levels: list[float]
    tp_pct: float
    buys: int = 0
    closed_cycles: int = 0
    wins: int = 0
    realized_pnl_usdt: float = 0.0
    floating_pnl_usdt: float = 0.0
    floating_pnl_pct: float = 0.0
    max_entries_used: int = 0
    open_position: bool = False
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total_pnl_usdt(self) -> float:
        return self.realized_pnl_usdt + self.floating_pnl_usdt

    @property
    def winrate(self) -> float | None:
        if self.closed_cycles <= 0:
            return None
        return self.wins / self.closed_cycles * 100.0


def _category(coin: str) -> str:
    return CATEGORIES.get(normalize_coin(coin), 'STRONG_ALT')


def _base_levels(coin: str) -> list[float]:
    cat = _category(coin)
    return list(BASE_DCA_LEVELS.get(cat, BASE_DCA_LEVELS['STRONG_ALT']))


def _make_dca_levels(coin: str, preset: str) -> list[float]:
    base = _base_levels(coin)
    factors = DCA_PRESETS[preset]
    levels = [round(base[i] * factors[i] * 2) / 2 for i in range(4)]
    for i in range(1, len(levels)):
        if levels[i] <= levels[i - 1]:
            levels[i] = levels[i - 1] + 3.0
    return levels


def _simulate_df(df: pd.DataFrame, coin: str, start: str, end: str, dca_levels: list[float], tp_pct: float) -> SimResult:
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)
    result = SimResult(coin=coin, category=_category(coin), preset='', dca_levels=dca_levels, tp_pct=tp_pct)

    qty = 0.0
    invested = 0.0
    avg_price = 0.0
    last_buy_price = 0.0
    entry_count = 0

    df = df.copy()
    df['rolling_high_30'] = df['high'].rolling(30).max().shift(1)

    for _, row in df.iterrows():
        dt = datetime.fromtimestamp(row['timestamp'] / 1000, tz=timezone.utc)
        if dt < start_dt or dt > end_dt:
            continue
        close = float(row['close'])
        high30 = float(row['rolling_high_30']) if pd.notna(row['rolling_high_30']) else 0.0
        if not high30:
            continue

        if qty <= 0:
            drawdown = (close / high30 - 1) * 100.0
            if drawdown <= -dca_levels[0]:
                buy_qty = ENTRY_USDT / close
                qty += buy_qty
                invested += ENTRY_USDT
                avg_price = invested / qty
                last_buy_price = close
                entry_count = 1
                result.buys += 1
                result.max_entries_used = max(result.max_entries_used, entry_count)
            continue

        pnl_pct = (close / avg_price - 1) * 100.0 if avg_price else 0.0
        if pnl_pct >= tp_pct:
            proceeds = qty * close
            pnl = proceeds - invested
            result.realized_pnl_usdt += pnl
            result.closed_cycles += 1
            if pnl > 0:
                result.wins += 1
            # reset cycle
            qty = 0.0
            invested = 0.0
            avg_price = 0.0
            last_buy_price = 0.0
            entry_count = 0
            continue

        if entry_count < len(dca_levels):
            next_drop = dca_levels[entry_count]
            next_buy_price = last_buy_price * (1 - next_drop / 100.0)
            if close <= next_buy_price:
                buy_qty = ENTRY_USDT / close
                qty += buy_qty
                invested += ENTRY_USDT
                avg_price = invested / qty
                last_buy_price = close
                entry_count += 1
                result.buys += 1
                result.max_entries_used = max(result.max_entries_used, entry_count)

    in_range = df[(df['date'] >= start) & (df['date'] <= end)]
    last_close = float(in_range.iloc[-1]['close']) if not in_range.empty else float(df.iloc[-1]['close'])
    if qty > 0:
        result.open_position = True
        result.floating_pnl_usdt = qty * last_close - invested
        result.floating_pnl_pct = (last_close / avg_price - 1) * 100.0 if avg_price else 0.0

    # Балл не равен доходности. Штрафуем открытые глубокие минусы и стратегии без закрытых циклов.
    score = result.total_pnl_usdt
    if result.open_position and result.floating_pnl_usdt < 0:
        score += result.floating_pnl_usdt * 0.5  # дополнительный штраф за зависшие минусы
    if result.closed_cycles == 0 and result.buys > 0:
        score -= 2.0
    if result.buys == 0:
        score -= 5.0
        result.notes.append('no_signals')
    if result.max_entries_used >= 4 and result.total_pnl_usdt <= 0:
        score -= 3.0
        result.notes.append('deep_dca_without_profit')
    result.score = round(score, 4)
    return result


def optimize_coin(coin: str, start: str, end: str, apply: bool = True) -> dict[str, Any]:
    coin = normalize_coin(coin)
    exchange = make_public_exchange()
    df = fetch_daily_history(exchange, coin, start, end, warmup_days=120)
    all_results: list[SimResult] = []
    for preset, tp in itertools.product(DCA_PRESETS.keys(), TP_CANDIDATES):
        levels = _make_dca_levels(coin, preset)
        res = _simulate_df(df, coin, start, end, levels, tp)
        res.preset = preset
        all_results.append(res)
    all_results.sort(key=lambda r: (r.score, r.total_pnl_usdt, r.closed_cycles), reverse=True)
    best = all_results[0]

    # TP уровни для живого бота: первый из оптимизации, дальше разумно шире.
    tp_levels = [best.tp_pct, max(best.tp_pct * 1.8, best.tp_pct + 10), max(best.tp_pct * 2.8, best.tp_pct + 25)]
    tp_levels = [round(x * 2) / 2 for x in tp_levels[:3]]
    if apply:
        update_coin_params(
            coin,
            best.dca_levels,
            tp_levels,
            meta={
                'period': f'{start} — {end}',
                'preset': best.preset,
                'score': best.score,
                'total_pnl_usdt': best.total_pnl_usdt,
                'realized_pnl_usdt': best.realized_pnl_usdt,
                'floating_pnl_usdt': best.floating_pnl_usdt,
                'closed_cycles': best.closed_cycles,
                'winrate': best.winrate,
                'generated_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
            },
        )

    return {
        'coin': coin,
        'category': best.category,
        'period': f'{start} — {end}',
        'best': _result_to_dict(best),
        'tp_levels': tp_levels,
        'top_results': [_result_to_dict(r) for r in all_results[:5]],
        'applied': apply,
    }


def _result_to_dict(r: SimResult) -> dict[str, Any]:
    return {
        'coin': r.coin,
        'category': r.category,
        'preset': r.preset,
        'dca_levels': r.dca_levels,
        'tp_pct': r.tp_pct,
        'buys': r.buys,
        'closed_cycles': r.closed_cycles,
        'wins': r.wins,
        'winrate': r.winrate,
        'realized_pnl_usdt': r.realized_pnl_usdt,
        'floating_pnl_usdt': r.floating_pnl_usdt,
        'floating_pnl_pct': r.floating_pnl_pct,
        'total_pnl_usdt': r.total_pnl_usdt,
        'max_entries_used': r.max_entries_used,
        'open_position': r.open_position,
        'score': r.score,
        'notes': r.notes,
    }


def run_strategy_lab(start: str, end: str, coin: str | None = None, apply: bool = True) -> dict[str, Any]:
    targets = [normalize_coin(coin)] if coin else list(COINS)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for c in targets:
        try:
            results.append(optimize_coin(c, start, end, apply=apply))
        except Exception as exc:
            errors.append({'coin': c, 'error': str(exc)})

    # CSV с результатами всех протестированных лучших параметров.
    REPORT_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    stamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    path = REPORT_DIR / f'strategy_lab_{stamp}.csv'
    with path.open('w', newline='', encoding='utf-8') as f:
        fieldnames = ['coin','category','period','preset','dca_levels','tp_pct','tp_levels','buys','closed_cycles','winrate','realized_pnl_usdt','floating_pnl_usdt','total_pnl_usdt','score','applied']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            b = row['best']
            writer.writerow({
                'coin': row['coin'],
                'category': row['category'],
                'period': row['period'],
                'preset': b['preset'],
                'dca_levels': '/'.join(str(x) for x in b['dca_levels']),
                'tp_pct': b['tp_pct'],
                'tp_levels': '/'.join(str(x) for x in row['tp_levels']),
                'buys': b['buys'],
                'closed_cycles': b['closed_cycles'],
                'winrate': '' if b['winrate'] is None else round(float(b['winrate']), 2),
                'realized_pnl_usdt': round(float(b['realized_pnl_usdt']), 4),
                'floating_pnl_usdt': round(float(b['floating_pnl_usdt']), 4),
                'total_pnl_usdt': round(float(b['total_pnl_usdt']), 4),
                'score': b['score'],
                'applied': row['applied'],
            })

    return {'start': start, 'end': end, 'coin': coin, 'results': results, 'errors': errors, 'report_path': str(path), 'applied': apply}


def calculate_entry_levels(symbol: str) -> dict[str, Any]:
    """Compute historical entry levels from 365d daily data.

    Calculates per-day drawdown from rolling 90d high and returns
    the 25th/50th/75th percentiles as t1/t2/t3 (in % below 90d high).
    """
    coin = normalize_coin(symbol)
    exchange = make_public_exchange()

    end = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    start = (datetime.now(timezone.utc) - timedelta(days=365)).strftime('%Y-%m-%d')

    df = fetch_daily_history(exchange, coin, start, end, warmup_days=90)

    df['rolling_max_90'] = df['high'].rolling(90).max().shift(1)

    start_ts = int(_parse_date(start).timestamp() * 1000)
    df_period = df[df['timestamp'] >= start_ts].copy()
    df_period = df_period.dropna(subset=['rolling_max_90'])
    df_period = df_period[df_period['rolling_max_90'] > 0]

    if df_period.empty:
        raise ValueError(f"Недостаточно данных для {coin}")

    drawdowns = (df_period['close'] / df_period['rolling_max_90'] - 1) * 100
    negative_dd = drawdowns[drawdowns < 0]

    if len(negative_dd) < 5:
        cat = CATEGORIES.get(coin, 'STRONG_ALT')
        base = BASE_DCA_LEVELS.get(cat, BASE_DCA_LEVELS['STRONG_ALT'])
        t1, t2, t3 = float(base[0]), float(base[1]), float(base[2])
    else:
        t1 = abs(float(negative_dd.quantile(0.25)))
        t2 = abs(float(negative_dd.quantile(0.50)))
        t3 = abs(float(negative_dd.quantile(0.75)))

    t1 = round(t1, 1)
    t2 = round(t2, 1)
    t3 = round(t3, 1)
    if t2 <= t1:
        t2 = round(t1 + 3.0, 1)
    if t3 <= t2:
        t3 = round(t2 + 5.0, 1)

    return {
        'coin': coin,
        't1': t1,
        't2': t2,
        't3': t3,
        'days_analyzed': int(len(df_period)),
        'negative_dd_days': int(len(negative_dd)),
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    }


def run_entry_levels(coins: list[str] | None = None) -> dict[str, Any]:
    """Compute entry levels for all coins and save to data/entry_levels.json."""
    targets = [normalize_coin(c) for c in (coins or COINS)]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if ENTRY_LEVELS_PATH.exists():
        try:
            existing = json.loads(ENTRY_LEVELS_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for coin in targets:
        try:
            r = calculate_entry_levels(coin)
            results[coin] = r
            existing[coin] = r
        except Exception as exc:
            errors[coin] = str(exc)

    if results:
        ENTRY_LEVELS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')

    return {'results': results, 'errors': errors}


def get_entry_levels(coin: str) -> dict[str, Any] | None:
    """Return saved entry levels for a coin, or None if not computed yet."""
    coin = normalize_coin(coin)
    if not ENTRY_LEVELS_PATH.exists():
        return None
    try:
        data = json.loads(ENTRY_LEVELS_PATH.read_text(encoding='utf-8'))
        return data.get(coin)
    except Exception:
        return None


def format_entry_levels_report(summary: dict[str, Any]) -> str:
    results = summary.get('results', {})
    errors = summary.get('errors', {})
    lines = ['📐 УРОВНИ ВХОДА (Entry Levels)', '']
    if results:
        lines.append(f"{'Монета':<8} {'T1%':>6} {'T2%':>6} {'T3%':>6} {'Дней':>5} {'Из них <0':>9}")
        lines.append('─' * 46)
        for coin, r in sorted(results.items()):
            lines.append(
                f"{r['coin']:<8} {r['t1']:>6.1f} {r['t2']:>6.1f} {r['t3']:>6.1f}"
                f" {r['days_analyzed']:>5} {r['negative_dd_days']:>9}"
            )
        lines += [
            '',
            'T1 = 25й перцентиль просадки от 90d max (мелкий вход)',
            'T2 = 50й перцентиль (средний вход)',
            'T3 = 75й перцентиль (глубокий вход)',
            f"Сохранено: {ENTRY_LEVELS_PATH}",
        ]
    if errors:
        lines += ['', '⚠️ Ошибки:']
        for coin, err in errors.items():
            lines.append(f"• {coin}: {err}")
    return '\n'.join(lines)


def format_strategy_lab_report(summary: dict[str, Any]) -> str:
    results = summary.get('results') or []
    errors = summary.get('errors') or []
    lines = [
        '🧪 STRATEGY LAB',
        f"Период: {summary.get('start')} — {summary.get('end')}",
        f"Режим: {'параметры применены в боте' if summary.get('applied') else 'только анализ'}",
        '',
    ]
    if not results:
        lines.append('Нет успешных результатов.')
    else:
        lines.append('📌 Лучшие параметры по монетам:')
        # sort by score desc
        results_sorted = sorted(results, key=lambda x: x['best']['score'], reverse=True)
        for row in results_sorted:
            b = row['best']
            dca = ' / '.join(f"{float(x):.0f}%" for x in b['dca_levels'])
            tp = ' / '.join(f"+{float(x):.0f}%" for x in row['tp_levels'])
            wr = '—' if b['winrate'] is None else f"{float(b['winrate']):.0f}%"
            lines.append(
                f"• {row['coin']}: DCA {dca} | TP {tp} | "
                f"pnl {float(b['total_pnl_usdt']):+.2f} | WR {wr} | score {float(b['score']):+.2f}"
            )
    if errors:
        lines += ['', '⚠️ Ошибки данных:']
        for e in errors[:8]:
            lines.append(f"• {e['coin']}: {e['error']}")
    lines += [
        '',
        'Что это значит:',
        '• Для новых монет бот использует максимум доступной истории Bybit, даже если полных 5 лет нет.',
        '• Strategy Lab не просто рисует отчет — он сохраняет лучшие DCA/TP в data/strategy_params.json.',
        '• После этого /scan, волатильность, DCA и подсказки TP начинают использовать эти параметры.',
        '• Это грубая оптимизация по дневным свечам: комиссии, спред и внутридневные проколы пока не учитываются.',
    ]
    return '\n'.join(lines)
