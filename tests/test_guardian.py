# tests/test_guardian.py

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from guardian import Guardian
from models import TradeSignal, SignalAction

# FIXTURES & MOCKS

@pytest.fixture
def mock_config():
    """Mock the global config to standardize position sizes during tests."""
    with patch('guardian.config') as mock_conf:
        mock_conf.pos_size = 0.10  # 10% position size
        yield mock_conf

@pytest.fixture
def mock_memory():
    """Mock StateMemory to return controlled cash and position data."""
    with patch('guardian.StateMemory') as MockMem:
        memory_instance = MockMem.return_value
        
        # Default starting state: $10,000 cash, no open positions
        memory_instance.get_balance.return_value = 10000.0
        memory_instance.get_all_positions.return_value = pd.DataFrame()
        
        yield memory_instance

@pytest.fixture
def base_signal():
    """A standard trade signal for testing."""
    return TradeSignal(
        action=SignalAction.BUY,
        ticker="TEST",
        insider="John Doe",
        title="CEO",
        price=150.0,
        value=500000.0,
        filed_days_ago=1
    )

# TEST CASES

def test_fractional_and_fallback_calculation(mock_config, mock_memory, base_signal):
    """Test that fractional shares and whole-share fallbacks are calculated correctly."""
    guardian = Guardian(broker=None)
    
    # Bypass yfinance fallback by providing a mock live price
    guardian._get_live_prices = MagicMock(return_value={"TEST": 150.0})
    
    # 10% of $10,000 = $1,000 target. 
    # $1,000 / $150 = 6.66666... shares.
    orders = guardian.process_signals([base_signal])
    
    assert len(orders) == 1
    assert orders[0].ticker == "TEST"
    assert orders[0].quantity == 6.6667       # Fractional primary (rounded to 4 decimals)
    assert orders[0].fallback_quantity == 6   # Whole share fallback

def test_insufficient_funds_rejection(mock_config, mock_memory, base_signal):
    """Test that signals are rejected if cash is lower than the target position size."""
    # Force cash balance to $50 (Target would be $5 based on 10%, but cash check fails the total equity sizing logic if we tweak it)
    mock_memory.get_balance.return_value = 50.0
    mock_memory.get_all_positions.return_value = pd.DataFrame([
        {'ticker': 'OTHER', 'quantity': 99.5, 'average_cost': 100.0}
    ])
    
    guardian = Guardian(broker=None)
    guardian._get_live_prices = MagicMock(return_value={"TEST": 150.0})
    
    orders = guardian.process_signals([base_signal])
    
    # Order should be refused due to insufficient cash
    assert len(orders) == 0

def test_zero_fallback_edge_case(mock_config, mock_memory):
    """Test behavior when the stock is extremely expensive, resulting in a 0 fallback quantity."""
    signal = TradeSignal(
        action=SignalAction.BUY,
        ticker="BRK.A",
        insider="Warren",
        title="CEO",
        price=600000.0,
        value=1000000.0,
        filed_days_ago=1
    )
    
    guardian = Guardian(broker=None)
    guardian._get_live_prices = MagicMock(return_value={"BRK.A": 600000.0})
    
    # 10% of $10,000 = $1,000 target.
    # $1,000 / $600,000 = 0.0017 shares.
    orders = guardian.process_signals([signal])
    
    assert len(orders) == 1
    assert orders[0].quantity == 0.0017
    assert orders[0].fallback_quantity == 0   # Fallback must be explicitly 0