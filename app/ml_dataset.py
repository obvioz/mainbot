from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.backtest import fetch_daily_history, make_public_exchange, DEFAULT_START, DEFAULT_END
from app.config import COINS, CATEGORIES, BASE_DCA_LEVELS, TAKE_PROFIT_LEVELS, normalize_coin
from app.storage import DATA_DIR

DATASET_EXPORT_PATH = DATA_DIR / "ml_signal_dataset.csv"


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _category(coin: str) -> str:
    return CATEGORIES.get(normalize_coin(coin), "STRONG_ALT")


def _levels(coin: str) -> list[float]:
    return list(BASE_DCA_LEVELS.get(_category(coin), BASE_DCA_LEVELS["STRONG_ALT"]))


def _tp(coin: str) -> float:
    return float(TAKE_PROFIT_LEVELS.get(_category(coin), TAKE_PROFIT_LEVELS["STRONG_ALT"])[0])


def _add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()
    df["atr14_pct"] = df["atr14"] / df["close"] * 100
    df["rolling_high_30"] = df["high"].rolling(30).max().shift(1)
    df["drawdown_30d_pct"] = (df["close"] / df["rolling_high_30"] - 1) * 100
    df["ret_1d_pct"] = df["close"].pct_change(1) * 100
    df["ret_7d_pct"] = df["close"].pct_change(7) * 100
    df["ret_30d_pct"] = df["close"].pct_change(30) * 100
    df["vol_avg20"] = df["volume"].rolling(20).mean()
    df["volume_ratio_20d"] = df["volume"] / df["vol_avg20"]
    return df


def _future_outcomes(df: pd.DataFrame, idx: int, tp_pct: float, adverse_pct: float, horizons: tuple[int, ...] = (14, 30)) -> dict[str, Any]:
    close = float(df.iloc[idx]["close"])
    result: dict[str, Any] = {}
    for h in horizons:
        future = df.iloc[idx + 1: idx + 1 + h]
        if future.empty:
            result[f"max_future_gain_{h}d_pct"] = None
            result[f"max_future_loss_{h}d_pct"] = None
            result[f"outcome_{h}d"] = "NO_DATA"
            continue
        max_gain = (float(future["high"].max()) / close - 1) * 100
        max_loss = (float(future["low"].min()) / close - 1) * 100
        result[f"max_future_gain_{h}d_pct"] = max_gain
        result[f"max_future_loss_{h}d_pct"] = max_loss
        hit_tp = max_gain >= tp_pct
        hit_adverse = max_loss <= -adverse_pct
        if hit_tp and not hit_adverse:
            outcome = "WIN"
        elif hit_adverse and not hit_tp:
            outcome = "LOSS"
        elif hit_tp and hit_adverse:
            outcome = "AMBIGUOUS_DAILY"
        else:
            outcome = "FLAT"
        result[f"outcome_{h}d"] = outcome
    return result


def build_ml_dataset_coin(coin: str, start: str = DEFAULT_START, end: str = DEFAULT_END, btc_df: pd.DataFrame | None = None) -> pd.DataFrame:
    coin = normalize_coin(coin)
    exchange = make_public_exchange()
    df = fetch_daily_history(exchange, coin, start, end)
    df = _add_features(df)

    if btc_df is None:
        btc_df = _add_features(fetch_daily_history(exchange, "BTC", start, end))
    btc_map = btc_df[["date", "ret_7d_pct", "ret_30d_pct"]].rename(columns={
        "ret_7d_pct": "btc_ret_7d_pct",
        "ret_30d_pct": "btc_ret_30d_pct",
    })
    df = df.merge(btc_map, on="date", how="left")
    df["relative_strength_7d_pct"] = df["ret_7d_pct"] - df["btc_ret_7d_pct"]
    df["relative_strength_30d_pct"] = df["ret_30d_pct"] - df["btc_ret_30d_pct"]

    start_dt = _parse_date(start)
    end_dt = _parse_date(end)
    levels = _levels(coin)
    first_level = float(levels[0])
    second_level = float(levels[1]) if len(levels) > 1 else first_level * 1.5
    tp_pct = _tp(coin)

    rows: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        dt = datetime.fromtimestamp(float(row["timestamp"]) / 1000, tz=timezone.utc)
        if dt < start_dt or dt > end_dt:
            continue
        drawdown = row.get("drawdown_30d_pct")
        if pd.isna(drawdown) or float(drawdown) > -first_level:
            continue
        base = {
            "coin": coin,
            "date": row["date"],
            "category": _category(coin),
            "close": float(row["close"]),
            "drawdown_30d_pct": float(drawdown),
            "atr14_pct": None if pd.isna(row.get("atr14_pct")) else float(row["atr14_pct"]),
            "volume_ratio_20d": None if pd.isna(row.get("volume_ratio_20d")) else float(row["volume_ratio_20d"]),
            "ret_1d_pct": None if pd.isna(row.get("ret_1d_pct")) else float(row["ret_1d_pct"]),
            "ret_7d_pct": None if pd.isna(row.get("ret_7d_pct")) else float(row["ret_7d_pct"]),
            "ret_30d_pct": None if pd.isna(row.get("ret_30d_pct")) else float(row["ret_30d_pct"]),
            "btc_ret_7d_pct": None if pd.isna(row.get("btc_ret_7d_pct")) else float(row["btc_ret_7d_pct"]),
            "btc_ret_30d_pct": None if pd.isna(row.get("btc_ret_30d_pct")) else float(row["btc_ret_30d_pct"]),
            "relative_strength_7d_pct": None if pd.isna(row.get("relative_strength_7d_pct")) else float(row["relative_strength_7d_pct"]),
            "relative_strength_30d_pct": None if pd.isna(row.get("relative_strength_30d_pct")) else float(row["relative_strength_30d_pct"]),
            "first_dca_level_pct": first_level,
            "next_dca_level_pct": second_level,
            "tp_pct": tp_pct,
        }
        base.update(_future_outcomes(df, idx, tp_pct=tp_pct, adverse_pct=second_level))
        rows.append(base)

    return pd.DataFrame(rows)


def build_ml_dataset_all(start: str = DEFAULT_START, end: str = DEFAULT_END) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    exchange = make_public_exchange()
    btc_df = _add_features(fetch_daily_history(exchange, "BTC", start, end))
    frames: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []
    for coin in COINS:
        try:
            frames.append(build_ml_dataset_coin(coin, start, end, btc_df=btc_df))
        except Exception as exc:
            errors.append({"coin": coin, "error": str(exc)})
    if frames:
        return pd.concat(frames, ignore_index=True), errors
    return pd.DataFrame(), errors


def export_ml_dataset(start: str = DEFAULT_START, end: str = DEFAULT_END, coin: str | None = None) -> tuple[Path, dict[str, Any]]:
    if coin:
        df = build_ml_dataset_coin(coin, start, end)
        errors: list[dict[str, str]] = []
        path = DATA_DIR / f"ml_signal_dataset_{normalize_coin(coin)}_{start}_{end}.csv"
    else:
        df, errors = build_ml_dataset_all(start, end)
        path = DATASET_EXPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, sep=";", encoding="utf-8-sig")
    summary = {
        "rows": int(len(df)),
        "coins": int(df["coin"].nunique()) if not df.empty and "coin" in df.columns else 0,
        "start": start,
        "end": end,
        "errors": errors,
    }
    if not df.empty:
        for h in (14, 30):
            col = f"outcome_{h}d"
            if col in df.columns:
                summary[f"outcome_{h}d"] = df[col].value_counts(dropna=False).to_dict()
    return path, summary


def format_ml_dataset_summary(summary: dict[str, Any]) -> str:
    lines = [
        "🤖 ML DATASET BUILDER",
        f"Период: {summary.get('start')} — {summary.get('end')}",
        f"Строк сигналов: {summary.get('rows', 0)}",
        f"Монет в датасете: {summary.get('coins', 0)}",
    ]
    for h in (14, 30):
        outcomes = summary.get(f"outcome_{h}d") or {}
        if outcomes:
            parts = [f"{k}: {v}" for k, v in outcomes.items()]
            lines.append(f"Outcome {h}d: " + ", ".join(parts))
    errors = summary.get("errors") or []
    if errors:
        lines.append("\n⚠️ Ошибки данных:")
        for e in errors[:8]:
            lines.append(f"• {e.get('coin')}: {e.get('error')}")
    lines += [
        "",
        "Это еще не ML-модель. Это датасет для обучения: признаки сигнала + результат через 14/30 дней.",
    ]
    return "\n".join(lines)
