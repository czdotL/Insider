from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class TradingConfig(BaseSettings):

    database_url: str = Field(..., description="PostgreSQL connection string")

    # Broker
    ibkr_account: str = Field(..., min_length=1, description="IBKR account identifier")

    # Filters & Logic
    q1_filter: bool
    liquidity_filter: bool
    market_filter: bool
    fscore_min: int = Field(..., ge=0, le=9, description="Piotroski F-score must be 0-9")

    # Delays (business days)
    entry_day_delay: int = Field(..., ge=0)
    entry_day_delay_max: int = Field(..., ge=0)
    paper_entry_delay_max: int = Field(..., ge=0)

    # Capital & Risk
    start_capital: int = Field(..., gt=0)
    pos_size: float = Field(..., gt=0.0, le=1.0, description="Position size as a percentage 0.01 to 1.0")

    # Execution Minimums
    min_trade_value: int = Field(..., ge=0)
    min_share_price: float = Field(..., ge=0.0)
    min_daily_volume: int = Field(..., ge=0)

    # Parsing Requires JSON format in .env
    titles: list[str]

    # Pre-Drop Strategy
    use_predrop: bool
    predrop_lookback: int = Field(..., gt=0, description="Lookback window in days")
    predrop_threshold: float = Field(..., le=0.0, description="Drop threshold as negative decimal")
    predrop_type: Literal['return', 'peak'] = Field(..., description="Strict type limit to prevent typos")

    # Trade Exits
    stoploss_pct: float = Field(..., gt=0.0, lt=1.0, description="Stop loss percentage 0.0 to 1.0")
    takeprofit_pct: float = Field(..., gt=0.0, description="Take profit percentage")
    max_hold_days: int = Field(..., gt=0)

    # Notification
    telegram_token: str = Field(..., min_length=10)
    telegram_chatid: int

    model_config = SettingsConfigDict(
        env_file='.env', 
        env_file_encoding='utf-8', 
        frozen=True,
        extra='ignore'  # Prevents crash if unrelated variables exist in the environment
    )

config = TradingConfig()