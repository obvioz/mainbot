import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_allowed_user_id: str = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")
    exchange_id: str = os.getenv("EXCHANGE_ID", "bybit")
    quote: str = os.getenv("QUOTE", "USDT")
    capital_usdt: float = float(os.getenv("CAPITAL_USDT", "1000"))
    scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
    auto_monitor_enabled: bool = os.getenv("AUTO_MONITOR_ENABLED", "true").lower() in {"1", "true", "yes", "y", "да"}
    bybit_api_key: str = os.getenv("BYBIT_API_KEY", "")
    bybit_api_secret: str = os.getenv("BYBIT_API_SECRET", "")
    bybit_sync_enabled: bool = os.getenv("BYBIT_SYNC_ENABLED", "false").lower() in {"1", "true", "yes", "y", "да"}
    bybit_portfolio_monitor_enabled: bool = os.getenv("BYBIT_PORTFOLIO_MONITOR_ENABLED", "true").lower() in {"1", "true", "yes", "y", "да"}
    bybit_portfolio_monitor_minutes: int = int(os.getenv("BYBIT_PORTFOLIO_MONITOR_MINUTES", "5"))
    bybit_deep_sync_minutes: int = int(os.getenv("BYBIT_DEEP_SYNC_MINUTES", "30"))
    bybit_recv_window: int = int(os.getenv("BYBIT_RECV_WINDOW", "30000"))

    auto_strategy_bootstrap_enabled: bool = os.getenv("AUTO_STRATEGY_BOOTSTRAP_ENABLED", "true").lower() in {"1", "true", "yes", "y", "да"}
    strategy_bootstrap_max_age_days: int = int(os.getenv("STRATEGY_BOOTSTRAP_MAX_AGE_DAYS", "7"))

    # Performance / memory
    market_cache_ttl_seconds: int = int(os.getenv("MARKET_CACHE_TTL_SECONDS", "60"))
    market_context_cache_ttl_seconds: int = int(os.getenv("MARKET_CONTEXT_CACHE_TTL_SECONDS", "180"))
    save_market_snapshots: bool = os.getenv("SAVE_MARKET_SNAPSHOTS", "true").lower() in {"1", "true", "yes", "y", "да"}
    bybit_time_safety_ms: int = int(os.getenv("BYBIT_TIME_SAFETY_MS", "1500"))

    # Trading keys (separate from read-only keys). Used for auto TP execution.
    # Leave empty to fall back to bybit_api_key/secret (must have Trade permission).
    bybit_trade_api_key: str = os.getenv("BYBIT_TRADE_API_KEY", "")
    bybit_trade_api_secret: str = os.getenv("BYBIT_TRADE_API_SECRET", "")
    # Set to true to enable automatic market sells on TP1 and trailing stop triggers.
    tp_auto_execute: bool = os.getenv("TP_AUTO_EXECUTE", "false").lower() in {"1", "true", "yes", "y", "да"}
    # Hard ceiling (USDT) on the value of a single AUTO order. Manual /buy and
    # button orders are NOT limited by this. Default 100 = our agreed auto-entry size.
    max_auto_order_usdt: float = float(os.getenv("MAX_AUTO_ORDER_USDT", "100"))

settings = Settings()
