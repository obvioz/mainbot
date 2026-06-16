# CODEX NOTES — Crypto Rotation Bot

## Project Summary

Telegram bot for crypto market analysis and semi-automatic trading research.

Current stack:

- Python 3.12
- aiogram
- ccxt
- Bybit Spot API
- local JSON storage
- Linux server + systemd
- Telegram UI

The bot is in active testing. Avoid large architecture rewrites and keep changes small, reversible, and easy to verify.

## Core Philosophy

This is not a high-frequency bot.

The goal is smart capital rotation:

- identify relative strength;
- rotate into stronger assets;
- accumulate BTC over time;
- survive drawdowns;
- avoid weak entries;
- preserve capital first.

## Current Architecture

Important files:

- `app/bot.py` — Telegram bot, commands, keyboards.
- `app/monitor.py` — background monitoring loop, market checks, rotation scheduler.
- `app/rotation_lab.py` — demo rotation engine, scoring, entry/exit, PnL, history.
- `app/market.py` — OHLCV and market data.
- `app/storage.py` — JSON persistence.
- `data/portfolio.json` — local portfolio source of truth for average prices.
- `data/rotation_lab.json` — demo rotation state.

## Implemented Features

- Bybit Unified Account balance reading.
- Spot position sync.
- Local portfolio tracking.
- Average price, PnL, invested USDT, current value.
- Market analysis.
- Portfolio tracking.
- Signals.
- Review, robustness tests, backtests.
- Demo rotation trading.

## Bybit Notes

Bybit API may not reliably return trade history after server migration.

Decision:

- local JSON portfolio is the primary source for average price;
- Bybit read-only sync updates positions where possible;
- do not depend on trade history as the only source of truth.

## Rotation Lab

Demo system rotates capital between assets without going through USDT.

Example:

`BTC -> ETH -> BTC`

The engine looks for coins strengthening relative to BTC.

Current demo commands:

- `/rotation`
- `/rotationtick`
- `/rotationhistory`
- `/rotationreset`

Current demo buttons:

- `🧪 Демо торговля`
- `📜 Демо журнал`

Current demo balance model:

- starts from `1 BTC`;
- tracks resulting BTC, PnL, history, winrate.

## Current Rotation Logic

The engine analyzes BTC pairs such as:

- `ETH/BTC`
- `SOL/BTC`

Scoring inputs include:

- momentum;
- volume impulse;
- 1h strength;
- 3h strength;
- volatility.

Current entry logic:

- score >= threshold;
- pair is rising against BTC;
- momentum is rising;
- volume impulse is present.

Current exit logic:

- take profit around `+1%`;
- stop loss around `-0.6%`;
- reverse signal.

## Critical Constraints

Do not:

- sharply rewrite architecture;
- break existing commands;
- remove legacy command support without a migration;
- introduce real trading before demo logic is statistically stable;
- make massive refactors during active testing.

Prefer:

- stabilization;
- focused tests;
- statistics collection;
- small, readable changes;
- compatibility with existing Telegram commands and server deployment.

## Local Work Done By Codex

Repository cloned locally from:

`https://github.com/obvioz/mainbot`

Local path:

`C:\Users\jkm18\Documents\Codex\2026-06-16\new-chat\mainbot`

### UI Cleanup, First Pass

Changed `app/bot.py`.

Main menu is now reduced to 4 sections:

- `📊 Анализ`
- `💼 Портфель`
- `🧪 Демо`
- `⚙️ Настройки`

Added section submenus and `⬅️ Главное меню`.

Existing command handlers and old text button handlers were kept for compatibility.

Also mapped `📦 Лог для ChatGPT` to the existing `/exportlog` behavior.

Verification:

- `python -m compileall app` passed using Codex bundled Python.

Important:

- These changes are local only.
- The production Telegram bot/server has not been restarted.
- Changes have not yet been committed or pushed unless a later note says otherwise.

## Near-Term Priorities

1. UI cleanup.
2. Better rotation scoring.
3. BTC/USDT market filter.
4. Cleaner notifications.
5. Rotation statistics.
6. Trade analytics.
7. Multi-pair expansion.
8. Realistic slippage/spread simulation.
9. Partial position sizing.
10. Auto pair discovery from Bybit.

## Next Suggested Task

Implement a BTC/USDT market filter in `app/rotation_lab.py`.

Problem:

`ETH/BTC` can rise while `BTC/USDT` is falling hard. In that situation ETH may be stronger than BTC, but both assets may still be falling against USD.

Desired behavior:

- if BTC/USDT is dropping sharply, do not enter new rotation trades;
- optionally include major coin / USDT trend before entry;
- do not force-close existing demo positions on the first version unless clearly justified;
- expose the reason in rotation summary/tick output.

Suggested first implementation:

- fetch BTC/USDT OHLCV;
- calculate recent BTC trend over a short window;
- define a conservative threshold;
- block new entries when BTC trend is below the threshold;
- keep the existing exit logic intact.

