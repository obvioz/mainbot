from __future__ import annotations
import math
from dataclasses import dataclass
import pandas as pd
from app.config import CATEGORIES, BASE_DCA_LEVELS
from app.strategy_params import get_coin_params

@dataclass(frozen=True)
class VolatilityProfile:
    atr_pct: float
    vol_7d_pct: float
    vol_30d_pct: float
    max_drawdown_90d_pct: float
    volatility_class: str
    dca_levels: list[float]
    comment: str

def _safe_float(x, default=0.0):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return default
        return float(x)
    except Exception:
        return default

def _max_drawdown_pct(closes: pd.Series) -> float:
    if closes.empty: return 0.0
    dd = closes / closes.cummax() - 1.0
    return float(dd.min() * 100.0)

def _atr_pct(df: pd.DataFrame, period=14) -> float:
    if len(df) < period + 1: return 0.0
    high, low, close = df['high'].astype(float), df['low'].astype(float), df['close'].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    current = close.iloc[-1]
    return float(atr / current * 100.0) if current > 0 else 0.0

def _realized_vol_pct(df: pd.DataFrame, window: int) -> float:
    if len(df) < window + 1: return 0.0
    r = df['close'].astype(float).pct_change().dropna().tail(window)
    return float(r.std() * 100.0) if not r.empty else 0.0

def _classify_volatility(coin: str, atr_pct: float, vol_30d_pct: float) -> str:
    cat = CATEGORIES.get(coin, 'STRONG_ALT')
    combined = max(atr_pct, vol_30d_pct * 2.0)
    if cat == 'CORE':
        return 'LOW' if combined < 3.5 else 'MEDIUM' if combined < 6.5 else 'HIGH'
    if cat == 'NARRATIVE':
        return 'MEDIUM' if combined < 6.0 else 'HIGH' if combined < 10.0 else 'EXTREME'
    return 'MEDIUM' if combined < 4.5 else 'HIGH' if combined < 8.0 else 'EXTREME'

def _adjust_dca_levels(coin: str, volatility_class: str, atr_pct: float) -> list[float]:
    cat = CATEGORIES.get(coin, 'STRONG_ALT')
    base = get_coin_params(coin).get('dca_levels') or BASE_DCA_LEVELS.get(cat, BASE_DCA_LEVELS['STRONG_ALT'])
    multiplier = {'LOW':0.9,'MEDIUM':1.0,'HIGH':1.15,'EXTREME':1.30}.get(volatility_class,1.0)
    min_first = max(base[0], round(atr_pct * 1.35, 1))
    levels=[]
    for i,lvl in enumerate(base):
        adj = lvl * multiplier
        if i == 0: adj = max(adj, min_first)
        adj = round(adj * 2) / 2
        levels.append(adj)
    for i in range(1,len(levels)):
        if levels[i] <= levels[i-1]: levels[i] = levels[i-1] + 3.0
    return levels[:4]

def analyze_volatility(df: pd.DataFrame, coin: str) -> VolatilityProfile:
    atr = _atr_pct(df,14)
    vol7 = _realized_vol_pct(df,7)
    vol30 = _realized_vol_pct(df,30)
    mdd90 = _max_drawdown_pct(df['close'].astype(float).tail(90))
    vclass = _classify_volatility(coin, atr, vol30)
    levels = _adjust_dca_levels(coin, vclass, atr)
    comment = {
        'LOW':'низкая/умеренная волатильность, лесенка может быть плотнее',
        'MEDIUM':'средняя волатильность, стандартная лесенка',
        'HIGH':'высокая волатильность, входы лучше разносить шире',
        'EXTREME':'экстремальная волатильность, ранние доборы опасны',
    }.get(vclass, 'волатильность не определена')
    return VolatilityProfile(_safe_float(atr), _safe_float(vol7), _safe_float(vol30), _safe_float(mdd90), vclass, levels, comment)

def get_static_next_dca_drop(coin: str, entry_count: int) -> float:
    cat = CATEGORIES.get(coin, 'STRONG_ALT')
    levels = get_coin_params(coin).get('dca_levels') or BASE_DCA_LEVELS.get(cat, BASE_DCA_LEVELS['STRONG_ALT'])
    idx = min(max(entry_count, 0), len(levels)-1)
    return float(levels[idx])
