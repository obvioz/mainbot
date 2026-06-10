from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import csv
import itertools
import json

import pandas as pd

from app.config import COINS, CATEGORIES, normalize_coin
from app.backtest import make_public_exchange, fetch_daily_history, _parse_date
from app.strategy_lab import DCA_PRESETS, TP_CANDIDATES, _make_dca_levels, _simulate_df, five_year_start
from app.strategy_params import update_coin_params

REPORT_DIR = Path('reports')
DATA_DIR = Path('data')
RELIABILITY_PATH = DATA_DIR / 'strategy_reliability.json'

MIN_TEST_DAYS = 90


@dataclass
class CandidateRobustResult:
    coin: str
    category: str
    preset: str
    dca_levels: list[float]
    tp_pct: float
    yearly_pnls: list[float]
    yearly_cycles: list[int]
    total_pnl: float
    total_cycles: int
    profitable_years: int
    tested_years: int
    winrate_proxy: float
    score: float
    reliability: float
    warnings: list[str]


def _category(coin: str) -> str:
    return CATEGORIES.get(normalize_coin(coin), 'STRONG_ALT')


def _year_ranges(start: str, end: str) -> list[tuple[str, str]]:
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)
    ranges: list[tuple[str, str]] = []
    for year in range(start_dt.year, end_dt.year + 1):
        a = max(start_dt, datetime(year, 1, 1, tzinfo=timezone.utc))
        b = min(end_dt, datetime(year, 12, 31, tzinfo=timezone.utc))
        if (b - a).days + 1 >= MIN_TEST_DAYS:
            ranges.append((a.strftime('%Y-%m-%d'), b.strftime('%Y-%m-%d')))
    return ranges


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _candidate_score(total_pnl: float, yearly_pnls: list[float], total_cycles: int) -> tuple[float, float, list[str]]:
    warnings: list[str] = []
    tested_years = len(yearly_pnls)
    profitable_years = sum(1 for x in yearly_pnls if x > 0)
    negative_years = sum(1 for x in yearly_pnls if x < 0)
    min_year = min(yearly_pnls) if yearly_pnls else 0.0
    avg_year = total_pnl / tested_years if tested_years else 0.0
    stability = (profitable_years / tested_years) if tested_years else 0.0

    score = total_pnl
    score += avg_year * 0.5
    score += stability * 10.0
    score -= negative_years * 4.0
    if min_year < 0:
        score += min_year * 0.7  # штраф за худший год
    if total_cycles < 3:
        score -= 8.0
        warnings.append('мало сделок')
    if tested_years < 3:
        score -= 6.0
        warnings.append('мало лет истории')
    if negative_years >= max(2, tested_years // 2):
        warnings.append('нестабильно по годам')

    # 0-100: это не вероятность, а рейтинг устойчивости.
    reliability = 45.0
    reliability += _clamp(total_pnl * 1.5, -25, 35)
    reliability += stability * 30.0
    reliability += _clamp(total_cycles * 1.5, 0, 15)
    reliability -= negative_years * 7.0
    if min_year < 0:
        reliability += _clamp(min_year * 1.2, -20, 0)
    reliability = _clamp(reliability, 0, 100)
    return round(score, 4), round(reliability, 1), warnings


def _simulate_candidate_by_year(df: pd.DataFrame, coin: str, periods: list[tuple[str, str]], preset: str, tp: float) -> CandidateRobustResult:
    levels = _make_dca_levels(coin, preset)
    yearly_pnls: list[float] = []
    yearly_cycles: list[int] = []
    for a, b in periods:
        r = _simulate_df(df, coin, a, b, levels, tp)
        yearly_pnls.append(float(r.total_pnl_usdt))
        yearly_cycles.append(int(r.closed_cycles))
    total_pnl = sum(yearly_pnls)
    total_cycles = sum(yearly_cycles)
    profitable_years = sum(1 for x in yearly_pnls if x > 0)
    tested_years = len(yearly_pnls)
    winrate_proxy = profitable_years / tested_years * 100.0 if tested_years else 0.0
    score, reliability, warnings = _candidate_score(total_pnl, yearly_pnls, total_cycles)
    return CandidateRobustResult(
        coin=coin,
        category=_category(coin),
        preset=preset,
        dca_levels=levels,
        tp_pct=tp,
        yearly_pnls=yearly_pnls,
        yearly_cycles=yearly_cycles,
        total_pnl=total_pnl,
        total_cycles=total_cycles,
        profitable_years=profitable_years,
        tested_years=tested_years,
        winrate_proxy=winrate_proxy,
        score=score,
        reliability=reliability,
        warnings=warnings,
    )


def _walk_forward(df: pd.DataFrame, coin: str, start: str, end: str) -> dict[str, Any]:
    periods = _year_ranges(start, end)
    if len(periods) < 3:
        return {'windows': [], 'total_pnl': 0.0, 'profitable_windows': 0, 'note': 'недостаточно лет для walk-forward'}

    windows: list[dict[str, Any]] = []
    # 2 года тренировки -> следующий год проверки.
    for i in range(2, len(periods)):
        train_start = periods[i - 2][0]
        train_end = periods[i - 1][1]
        test_start, test_end = periods[i]

        train_results: list[Any] = []
        for preset, tp in itertools.product(DCA_PRESETS.keys(), TP_CANDIDATES):
            levels = _make_dca_levels(coin, preset)
            train_results.append(_simulate_df(df, coin, train_start, train_end, levels, tp))
        train_results.sort(key=lambda r: (r.score, r.total_pnl_usdt, r.closed_cycles), reverse=True)
        best_train = train_results[0]
        test_result = _simulate_df(df, coin, test_start, test_end, best_train.dca_levels, best_train.tp_pct)
        windows.append({
            'train': f'{train_start} — {train_end}',
            'test': f'{test_start} — {test_end}',
            'preset': best_train.preset,
            'dca_levels': best_train.dca_levels,
            'tp_pct': best_train.tp_pct,
            'test_pnl': test_result.total_pnl_usdt,
            'test_cycles': test_result.closed_cycles,
            'test_winrate': test_result.winrate,
        })

    total = sum(float(w['test_pnl']) for w in windows)
    profitable = sum(1 for w in windows if float(w['test_pnl']) > 0)
    return {'windows': windows, 'total_pnl': total, 'profitable_windows': profitable}


def _to_dict(r: CandidateRobustResult) -> dict[str, Any]:
    return {
        'coin': r.coin,
        'category': r.category,
        'preset': r.preset,
        'dca_levels': r.dca_levels,
        'tp_pct': r.tp_pct,
        'tp_levels': [r.tp_pct, max(r.tp_pct * 1.8, r.tp_pct + 10), max(r.tp_pct * 2.8, r.tp_pct + 25)],
        'yearly_pnls': [round(x, 4) for x in r.yearly_pnls],
        'yearly_cycles': r.yearly_cycles,
        'total_pnl': round(r.total_pnl, 4),
        'total_cycles': r.total_cycles,
        'profitable_years': r.profitable_years,
        'tested_years': r.tested_years,
        'winrate_proxy': round(r.winrate_proxy, 2),
        'score': r.score,
        'reliability': r.reliability,
        'warnings': r.warnings,
    }


def analyze_coin_robustness(coin: str, start: str, end: str, apply: bool = True) -> dict[str, Any]:
    coin = normalize_coin(coin)
    exchange = make_public_exchange()
    df = fetch_daily_history(exchange, coin, start, end, warmup_days=160)
    periods = _year_ranges(start, end)
    if not periods:
        raise ValueError('Недостаточно истории для robust-анализа')

    candidates: list[CandidateRobustResult] = []
    for preset, tp in itertools.product(DCA_PRESETS.keys(), TP_CANDIDATES):
        candidates.append(_simulate_candidate_by_year(df, coin, periods, preset, tp))
    candidates.sort(key=lambda r: (r.reliability, r.score, r.total_pnl), reverse=True)
    best = candidates[0]
    wf = _walk_forward(df, coin, start, end)

    tp_levels = [best.tp_pct, max(best.tp_pct * 1.8, best.tp_pct + 10), max(best.tp_pct * 2.8, best.tp_pct + 25)]
    tp_levels = [round(float(x) * 2) / 2 for x in tp_levels[:3]]

    if apply:
        update_coin_params(
            coin,
            best.dca_levels,
            tp_levels,
            meta={
                'source': 'robustness_engine',
                'period': f'{start} — {end}',
                'preset': best.preset,
                'robust_score': best.score,
                'reliability': best.reliability,
                'total_pnl_usdt': best.total_pnl,
                'profitable_years': best.profitable_years,
                'tested_years': best.tested_years,
                'total_cycles': best.total_cycles,
                'winrate_proxy': best.winrate_proxy,
                'walk_forward_total_pnl': wf.get('total_pnl'),
                'walk_forward_profitable_windows': wf.get('profitable_windows'),
                'generated_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
                'warnings': best.warnings,
            },
        )

    return {
        'coin': coin,
        'category': best.category,
        'period': f'{start} — {end}',
        'best': _to_dict(best),
        'top_results': [_to_dict(x) for x in candidates[:5]],
        'walk_forward': wf,
        'applied': apply,
    }


def run_robustness_lab(start: str | None = None, end: str | None = None, coin: str | None = None, apply: bool = True) -> dict[str, Any]:
    end = end or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    start = start or five_year_start(end)
    targets = [normalize_coin(coin)] if coin else list(COINS)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for c in targets:
        try:
            results.append(analyze_coin_robustness(c, start, end, apply=apply))
        except Exception as exc:
            errors.append({'coin': c, 'error': str(exc)})

    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    ranking = {
        'version': 1,
        'generated_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
        'period': f'{start} — {end}',
        'coins': {r['coin']: r['best'] for r in results},
        'errors': errors,
    }
    RELIABILITY_PATH.write_text(json.dumps(ranking, ensure_ascii=False, indent=2), encoding='utf-8')

    stamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    csv_path = REPORT_DIR / f'robustness_lab_{stamp}.csv'
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        fieldnames = ['coin','category','period','reliability','robust_score','dca_levels','tp_pct','total_pnl','profitable_years','tested_years','total_cycles','wf_total_pnl','warnings']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            b = r['best']
            wf = r.get('walk_forward') or {}
            writer.writerow({
                'coin': r['coin'],
                'category': r['category'],
                'period': r['period'],
                'reliability': b['reliability'],
                'robust_score': b['score'],
                'dca_levels': '/'.join(str(x) for x in b['dca_levels']),
                'tp_pct': b['tp_pct'],
                'total_pnl': b['total_pnl'],
                'profitable_years': b['profitable_years'],
                'tested_years': b['tested_years'],
                'total_cycles': b['total_cycles'],
                'wf_total_pnl': round(float(wf.get('total_pnl') or 0), 4),
                'warnings': '; '.join(b.get('warnings') or []),
            })

    return {'start': start, 'end': end, 'coin': coin, 'results': results, 'errors': errors, 'report_path': str(csv_path), 'applied': apply}


def load_reliability() -> dict[str, Any]:
    if not RELIABILITY_PATH.exists():
        return {'version': 1, 'coins': {}, 'errors': []}
    try:
        return json.loads(RELIABILITY_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'version': 1, 'coins': {}, 'errors': []}


def get_reliability_score(coin: str) -> float | None:
    data = load_reliability()
    row = (data.get('coins') or {}).get(normalize_coin(coin))
    if not row:
        return None
    try:
        return float(row.get('reliability'))
    except Exception:
        return None


def format_reliability_report(limit: int = 20) -> str:
    data = load_reliability()
    coins = data.get('coins') or {}
    if not coins:
        return '🧬 RELIABILITY\n\nРейтинга надежности пока нет. Запусти /robust 5y.'
    rows = sorted(coins.values(), key=lambda x: float(x.get('reliability') or 0), reverse=True)
    lines = ['🧬 RELIABILITY RANKING', f"Период: {data.get('period', '—')}", '']
    for r in rows[:limit]:
        warn = r.get('warnings') or []
        w = f" ⚠️ {', '.join(warn[:2])}" if warn else ''
        lines.append(
            f"• {r['coin']}: {float(r.get('reliability') or 0):.0f}/100 | "
            f"{r.get('profitable_years')}/{r.get('tested_years')} лет + | "
            f"pnl {float(r.get('total_pnl') or 0):+.2f} | "
            f"DCA {'/'.join(str(int(x)) for x in r.get('dca_levels', []))} | TP +{float(r.get('tp_pct') or 0):.0f}%{w}"
        )
    lines += ['', 'Смысл: это не прогноз, а устойчивость параметров на разных кусках истории. Чем ниже рейтинг, тем осторожнее бот должен относиться к BUY-сигналам.']
    return '\n'.join(lines)


def format_robustness_report(summary: dict[str, Any]) -> str:
    results = summary.get('results') or []
    errors = summary.get('errors') or []
    lines = [
        '🧬 STRATEGY ROBUSTNESS',
        f"Период: {summary.get('start')} — {summary.get('end')}",
        f"Режим: {'параметры применены в боте' if summary.get('applied') else 'только анализ'}",
        '',
    ]
    if not results:
        lines.append('Нет успешных результатов.')
    else:
        lines.append('📌 Рейтинг устойчивости:')
        results_sorted = sorted(results, key=lambda x: x['best']['reliability'], reverse=True)
        for row in results_sorted:
            b = row['best']
            wf = row.get('walk_forward') or {}
            dca = ' / '.join(f"{float(x):.0f}%" for x in b['dca_levels'])
            warnings = b.get('warnings') or []
            warn = f" | ⚠️ {', '.join(warnings[:2])}" if warnings else ''
            lines.append(
                f"• {row['coin']}: reliability {float(b['reliability']):.0f}/100 | "
                f"{b['profitable_years']}/{b['tested_years']} лет + | "
                f"pnl {float(b['total_pnl']):+.2f} | WF {float(wf.get('total_pnl') or 0):+.2f} | "
                f"DCA {dca} | TP +{float(b['tp_pct']):.0f}%{warn}"
            )
    if errors:
        lines += ['', '⚠️ Ошибки данных:']
        for e in errors[:8]:
            lines.append(f"• {e['coin']}: {e['error']}")
    lines += [
        '',
        'Что изменилось:',
        '• Бот больше не выбирает параметры только по общей доходности.',
        '• Он штрафует мало сделок, минусовые годы и нестабильность.',
        '• Walk-forward показывает, как параметры работали на следующем периоде, а не только внутри подгонки.',
        '• Рейтинг сохраняется в data/strategy_reliability.json и параметры — в data/strategy_params.json.',
    ]
    return '\n'.join(lines)
