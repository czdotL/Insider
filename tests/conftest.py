import asyncio
import os

_TEST_ENV_DEFAULTS = {
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "IBKR_ACCOUNT": "TEST",
    "IBKR_HOST": "127.0.0.1",
    "IBKR_PORT": "4002",
    "Q1_FILTER": "false",
    "LIQUIDITY_FILTER": "false",
    "MARKET_FILTER": "false",
    "FSCORE_MIN": "7",
    "ENTRY_DAY_DELAY": "0",
    "ENTRY_DAY_DELAY_MAX": "5",
    "PAPER_ENTRY_DELAY_MAX": "5",
    "START_CAPITAL": "1000",
    "POS_SIZE": "0.1",
    "MIN_TRADE_VALUE": "0",
    "MIN_SHARE_PRICE": "0",
    "MIN_DAILY_VOLUME": "0",
    "TITLES": '["CEO"]',
    "USE_PREDROP": "false",
    "PREDROP_LOOKBACK": "10",
    "PREDROP_THRESHOLD": "-0.1",
    "PREDROP_TYPE": "return",
    "STOPLOSS_PCT": "0.3",
    "TAKEPROFIT_PCT": "5.0",
    "MAX_HOLD_DAYS": "30",
    "TELEGRAM_TOKEN": "test_token_1234567890",
    "TELEGRAM_CHATID": "123456",
}

# TradingConfig() is instantiated as an eager module-level singleton the moment
# config.py is imported, which happens transitively from almost every module.
# Tests must not depend on (or accidentally read) a developer's real .env, so
# fill in harmless placeholders for any required field not already present in
# the environment before any test module gets imported.
for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


def pytest_configure(config):
    """Ensure an asyncio event loop is active before module collection."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)