# tests/test_exiter.py

import pytest
import pandas as pd
from unittest.mock import patch
from datetime import datetime, timedelta
from exiter import Exiter

@pytest.fixture
def mock_exiter_config():
    """Mock configuration parameters required by Exiter."""
    with patch('exiter.config') as mock_conf:
        mock_conf.max_hold_days = 30
        yield mock_conf

@patch('exiter.StateMemory')
def test_get_exit_signals_time_limit(mock_memory_cls, mock_exiter_config):
    """Test that a position exceeding max hold days generates a time exit signal."""
    memory_instance = mock_memory_cls.return_value
    
    # Position entered 50 calendar days ago (~35 business days)
    old_entry_date = (datetime.now() - timedelta(days=50)).strftime('%Y-%m-%d')
    
    memory_instance.get_all_positions.return_value = pd.DataFrame([{
        'ticker': 'TEST',
        'quantity': 10.0,
        'average_cost': 100.0,
        'entry_date': old_entry_date
    }])
    
    exiter = Exiter()
    signals = exiter.get_exit_signals()
    
    assert len(signals) == 1
    assert signals[0].ticker == 'TEST'
    assert signals[0].quantity == 10.0

@patch('exiter.StateMemory')
def test_get_exit_signals_within_limit(mock_memory_cls, mock_exiter_config):
    """Test that a recent position does not trigger a time exit signal."""
    memory_instance = mock_memory_cls.return_value
    
    recent_entry_date = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
    
    memory_instance.get_all_positions.return_value = pd.DataFrame([{
        'ticker': 'TEST',
        'quantity': 10.0,
        'average_cost': 100.0,
        'entry_date': recent_entry_date
    }])
    
    exiter = Exiter()
    signals = exiter.get_exit_signals()
    
    assert len(signals) == 0