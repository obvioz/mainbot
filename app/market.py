import ccxt
import pandas as pd
from app.settings import settings
from app.config import COINS, CATEGORIES, normalize_coin
from app.volatility import analyze_volatility
from app.derivatives import fetch_derivatives_snapshot
from app.news_risk import analyze_news_risk


def make_exchange():
    exchange_cls = getattr(ccxt, settings.exchange_id)
    return exchange_cls({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
            "recvWindow": settings.bybit_recv_window,
        },
    })


def symbol_for_coin(coin: str) -> str:
    coin = normalize_coin(coin)
    return f"{coin}/{settings.quote}"


def fetch_ohlcv_df(exchange, symbol: str, timeframe: str = "1d", limit: int = 90) -> pd.DataFrame:
    last_exc = None
    for _ in range(3):
        try:
            rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            break
        except Exception as exc:
            last_exc = exc
    else:
        raise last_exc
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def analyze_coin(exchange, coin: str) -> dict:
    coin = normalize_coin(coin)
    symbol = symbol_for_coin(coin)
    df = fetch_ohlcv_df(exchange, symbol, "1d", 90)
    if len(df) < 31:
        raise ValueError(f"Недостаточно данных по {symbol}")

    current = float(df.iloc[-1]["close"])
    prev = float(df.iloc[-2]["close"])
    seven_ago = float(df.iloc[-8]["close"])
    high_30d = float(df["high"].tail(30).max())
    low_30d = float(df["low"].tail(30).min())
    high_90d = float(df["high"].max())
    low_90d = float(df["low"].min())

    vol = analyze_volatility(df, coin)
    derivatives = fetch_derivatives_snapshot(exchange, coin)
    news_risk = analyze_news_risk(coin)

    return {
        "coin": coin,
        "symbol": symbol,
        "category": CATEGORIES.get(coin, "STRONG_ALT"),
        "current": current,
        "change_24h": (current / prev - 1) * 100,
        "change_7d": (current / seven_ago - 1) * 100,
        "drawdown_30d_high": (current / high_30d - 1) * 100,
        "drawdown_90d_high": (current / high_90d - 1) * 100,
        "distance_30d_low": (current / low_30d - 1) * 100,
        "high_30d": high_30d,
        "low_30d": low_30d,
        "high_90d": high_90d,
        "low_90d": low_90d,
        "atr_pct": vol.atr_pct,
        "vol_7d_pct": vol.vol_7d_pct,
        "vol_30d_pct": vol.vol_30d_pct,
        "max_drawdown_90d_pct": vol.max_drawdown_90d_pct,
        "volatility_class": vol.volatility_class,
        "dca_levels": vol.dca_levels,
        "volatility_comment": vol.comment,
        "derivatives": derivatives,
        "news_risk": news_risk,
    }


def analyze_market() -> list[dict]:
    exchange = make_exchange()
    results = []
    for coin in COINS:
        try:
            results.append(analyze_coin(exchange, coin))
        except Exception as exc:
            results.append({"coin": coin, "error": str(exc)})
    return results
