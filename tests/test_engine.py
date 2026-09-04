# tests/test_engine.py

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from engine import StrategyEngine

@pytest.fixture
def mock_engine_config():
    """Mock the global config for the StrategyEngine."""
    with patch('engine.config') as mock_conf:
        mock_conf.market_filter = True
        mock_conf.predrop_lookback = 10
        mock_conf.predrop_threshold = -0.10
        mock_conf.predrop_type = 'return'
        yield mock_conf


@pytest.fixture(autouse=True)
def mock_engine_state_memory():
    """StrategyEngine's constructor unconditionally builds a StateMemory (real DB
    connection). These tests only exercise filtering logic, so the DB dependency
    is mocked out for every test in this module."""
    with patch('engine.StateMemory') as mock_memory_cls:
        yield mock_memory_cls

@patch('engine.yf.Ticker')
def test_is_market_bullish_uptrend(mock_yf_ticker, mock_engine_config):
    """Test that the market filter approves trading when SPY is above its 200 SMA."""
    # Create 201 days of price data.
    # Days 1-199: $100. Day 200 (yesterday): $150. Day 201 (today): $160.
    # The 200-day moving average on yesterday will be ~ $100.25.
    # Yesterday's close ($150) > MA200 ($100.25) -> Bullish.
    mock_df = pd.DataFrame({'Close': [100.0] * 199 + [150.0, 160.0]})
    mock_yf_ticker.return_value.history.return_value = mock_df

    engine = StrategyEngine()

    assert engine.is_market_bullish() is True

@patch('engine.yf.Ticker')
def test_is_market_bullish_downtrend(mock_yf_ticker, mock_engine_config):
    """Test that the market filter suspends trading when SPY is below its 200 SMA."""
    # Days 1-199: $200. Day 200 (yesterday): $150.
    # MA200 will be ~ $199.75. $150 < $199.75 -> Bearish.
    mock_df = pd.DataFrame({'Close': [200.0] * 199 + [150.0, 160.0]})
    mock_yf_ticker.return_value.history.return_value = mock_df

    engine = StrategyEngine()

    assert engine.is_market_bullish() is False

def test_market_filter_disabled(mock_engine_config):
    """Test that trading is always approved if the market filter is toggled off."""
    mock_engine_config.market_filter = False
    engine = StrategyEngine()

    # Should return True immediately without calling yfinance
    assert engine.is_market_bullish() is True

@patch('engine.yf.Ticker')
def test_check_pre_drop_passed(mock_yf_ticker, mock_engine_config):
    """Test that a stock is approved if it dropped more than the threshold."""
    # 12 days of data. Lookback is 10 days.
    # 11 days ago price: $100. Today's price: $85. Drop = -15% (Passes -10% threshold)
    mock_df = pd.DataFrame({'Close': [100.0] * 11 + [85.0]})
    mock_yf_ticker.return_value.history.return_value = mock_df

    engine = StrategyEngine()
    passed, drop_pct = engine._check_pre_drop("TEST")

    assert passed is True
    assert round(drop_pct, 2) == -0.15

@patch('engine.yf.Ticker')
def test_check_pre_drop_failed(mock_yf_ticker, mock_engine_config):
    """Test that a stock is rejected if it did not drop enough."""
    # 11 days ago price: $100. Today's price: $95. Drop = -5% (Fails -10% threshold)
    mock_df = pd.DataFrame({'Close': [100.0] * 11 + [95.0]})
    mock_yf_ticker.return_value.history.return_value = mock_df

    engine = StrategyEngine()
    passed, drop_pct = engine._check_pre_drop("TEST")

    assert passed is False
    assert round(drop_pct, 2) == -0.05