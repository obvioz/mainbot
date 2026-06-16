# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Activate virtualenv first
source venv/bin/activate

# Run the bot
python main.py
```

The bot requires a `.env` file in the project root with the following variables:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_ID=...
BYBIT_API_KEY=...           # read-only key only
BYBIT_API_SECRET=...
CAPITAL_USDT=1000
SCAN_INTERVAL_MINUTES=15
AUTO_MONITOR_ENABLED=true
BYBIT_RECV_WINDOW=30000
BYBIT_TIME_SAFETY_MS=1500   # increase to 2500 if retCode 10002 errors appear
```

There are no tests and no lint commands configured.

## Architecture

Entry point is `main.py` → `app/bot.py:main()`, which wires aiogram handlers and starts two background asyncio tasks.

### Core data flow

```
Bybit (ccxt) → app/market.py → app/signals.py → Telegram (aiogram)
```

- `app/market.py` — fetches OHLCV from Bybit via `ccxt`, computes technical indicators per coin. `analyze_market()` runs all coins from `app/config.COINS`. `make_exchange()` creates the ccxt instance.
- `app/signals.py` — classifies each coin into `STRONG_BUY / ACCUMULATION / WATCH / NO_SIGNAL / AVOID` using market regime, DCA levels, reliability scores, and derivatives data.
- `app/market_intelligence.py` — builds macro market context (Fear & Greed, BTC dominance, alt basket) used to adjust signal scoring.
- `app/config.py` — defines the watchlist (`COINS`), categories (`CORE / STRONG_ALT / NARRATIVE`), DCA ladder levels, TP levels, and allocation targets.

### Portfolio and storage

- `app/storage.py` — local JSON portfolio (`data/portfolio_state.json`) and trade journal (`data/journal.json`). This is the **local** state; Bybit is the source of truth.
- `app/bybit_sync.py` — syncs Bybit spot holdings into the local portfolio via read-only API.
- `app/bybit_portfolio_monitor.py` — detects new buys/sells on Bybit and notifies via Telegram.
- `app/settings.py` — all config loaded from `.env` via `python-dotenv`; a frozen dataclass singleton at `settings`.

### Background tasks (started in `bot.py:main()`)

- `app/monitor.py:monitor_loop()` — periodic market scan; alerts on `STRONG_BUY / ACCUMULATION` signals and DCA/TP triggers for open positions. Also calls `rotation_tick()`.
- `app/bootstrap.py:auto_strategy_bootstrap()` — runs `/robust` + `/lab` automatically if strategy params are older than `STRATEGY_BOOTSTRAP_MAX_AGE_DAYS` (default 7).

### Strategy optimization

- `app/strategy_lab.py` — brute-forces DCA/TP parameter combinations on historical daily OHLCV; saves best params to `data/strategy_params.json`.
- `app/strategy_robustness.py` — walk-forward robustness check; saves reliability scores to `data/strategy_reliability.json`. Reliability scores feed back into `signals.py` to adjust signal strength.
- `app/strategy_params.py` — reads saved params; `get_tp_levels(coin)` is used by `monitor.py` for TP alerts.

### Other modules

- `app/derivatives.py` — fetches perpetual funding rates and open interest from Bybit.
- `app/news_risk.py` — RSS-based news risk scoring per coin.
- `app/market_cache.py` — in-process TTL cache used in `bot.py` to avoid hammering Bybit on every Telegram command (`MARKET_CACHE_TTL_SECONDS`, `MARKET_CONTEXT_CACHE_TTL_SECONDS`).
- `app/market_memory.py` — stores scan snapshots to `data/market_snapshots.jsonl`; `snapshot_delta()` powers the "📈 Изменения" report.
- `app/rotation_lab.py` — paper-trading rotation simulation (demo mode only).
- `app/backtest.py` — simple daily-close backtest engine.
- `app/ml_dataset.py` — exports feature + outcome CSV for offline ML training.

### Data directory (`data/`)

All JSON/JSONL state files live here. Key files:
- `portfolio_state.json` — local open positions
- `journal.json` — trade history
- `strategy_params.json` — optimized DCA/TP params (written by `/lab`)
- `strategy_reliability.json` — robustness scores (written by `/robust`)
- `market_snapshots.jsonl` — rolling scan history for delta reports
- `monitor_state.json` — last-seen signal keys to suppress duplicate alerts

### Access control

All command handlers call `is_allowed(message)` first. If `TELEGRAM_ALLOWED_USER_ID` is set in `.env`, only that user ID can interact with the bot.
