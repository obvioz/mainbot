# v25.1 Bybit Time Fix

Что исправлено:

- усиленная синхронизация времени Bybit для приватных запросов;
- `BYBIT_RECV_WINDOW` по умолчанию 30000;
- добавлен `BYBIT_TIME_SAFETY_MS` — бот отправляет timestamp немного позади server time;
- приватные запросы Bybit повторяются после повторной синхронизации времени;
- `fetch_balance` и `fetch_my_trades` передают `recvWindow` явно.

Рекомендуемые строки в `.env`:

```env
BYBIT_RECV_WINDOW=30000
BYBIT_TIME_SAFETY_MS=1500
```

Если снова появится ошибка `retCode 10002`, увеличь:

```env
BYBIT_TIME_SAFETY_MS=2500
```

Потом перезапусти:

```bash
python main.py
```
