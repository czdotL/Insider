from dataclasses import dataclass
from enum import Enum

class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

@dataclass(frozen=True, slots=True)
class TradeSignal:
    action: SignalAction
    ticker: str
    insider: str
    title: str
    price: float
    value: float
    filed_days_ago: int
    pre_drop_pct: float | None = None