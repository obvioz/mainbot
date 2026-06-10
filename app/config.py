# Монеты из нашей стратегии
COINS = [
    "BTC", "ETH", "SOL", "BNB", "LINK", "AAVE",
    "AVAX", "SUI", "NEAR", "RENDER", "FET", "TON"
]

# Алиасы старых тикеров. RNDR на Bybit торгуется как RENDER.
COIN_ALIASES = {
    "RNDR": "RENDER",
}

def normalize_coin(coin: str) -> str:
    coin = (coin or "").upper().strip()
    return COIN_ALIASES.get(coin, coin)

CATEGORIES = {
    "BTC": "CORE",
    "ETH": "CORE",
    "SOL": "STRONG_ALT",
    "BNB": "STRONG_ALT",
    "LINK": "STRONG_ALT",
    "AAVE": "STRONG_ALT",
    "AVAX": "STRONG_ALT",
    "SUI": "STRONG_ALT",
    "NEAR": "STRONG_ALT",
    "TON": "STRONG_ALT",
    "RENDER": "NARRATIVE",
    "FET": "NARRATIVE",
}

# Мягкие ориентиры позиции в USDT. Это НЕ общий капитал и НЕ жесткий запрет.
# Нужны только для расчета примерной суммы входа и предупреждений о концентрации.
ALLOCATION_USDT = {
    "BTC": 200,
    "ETH": 150,
    "SOL": 90,
    "BNB": 80,
    "LINK": 80,
    "AAVE": 80,
    "AVAX": 70,
    "SUI": 70,
    "NEAR": 60,
    "TON": 60,
    "RENDER": 50,
    "FET": 50,
}

# Лесенка входов: 4 покупки от лимита на монету.
ENTRY_STEPS = [0.15, 0.20, 0.30, 0.35]

# Базовая DCA-лесенка по типу монеты.
# Значения — % падения, после которого есть смысл рассматривать вход/добор.
BASE_DCA_LEVELS = {
    "CORE": [7.0, 10.0, 15.0, 22.0],
    "STRONG_ALT": [12.0, 16.0, 25.0, 35.0],
    "NARRATIVE": [15.0, 25.0, 40.0, 55.0],
}

# Фиксация прибыли по типам монет.
TAKE_PROFIT_LEVELS = {
    "CORE": [12.0, 25.0, 40.0],
    "STRONG_ALT": [15.0, 30.0, 50.0],
    "NARRATIVE": [20.0, 40.0, 70.0],
}

# Защитные триггеры рынка.
BTC_PANIC_24H = -6.0      # если BTC падает сильнее 6% за сутки — осторожность

