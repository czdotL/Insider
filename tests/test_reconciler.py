# tests/test_reconciler.py

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from reconciler import Reconciler

@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.is_connected.return_value = True
    broker.get_account_summary.return_value = 15000.0
    broker.get_positions.return_value = [
        {'symbol': 'AAPL', 'qty': 5.0, 'avg_cost': 150.0}
    ]
    broker.get_open_orders.return_value = []
    broker.get_recent_fills.return_value = []
    broker.get_open_orders_detailed.return_value = []
    return broker

@patch('reconciler.StateMemory')
@patch('reconciler.HistoryLogger')
@patch('reconciler.TelegramNotifier')
def test_sync_data_updates_balance_and_positions(mock_notifier_cls, mock_history_cls, mock_memory_cls, mock_broker):
    memory_instance = mock_memory_cls.return_value
    memory_instance.get_balance.return_value = 10000.0
    memory_instance.get_all_positions.return_value = pd.DataFrame()

    reconciler = Reconciler(broker=mock_broker)
    reconciler.sync_data()

    memory_instance.update_balance.assert_called_once_with(15000.0)
    memory_instance.update_position.assert_called_once_with('AAPL', 5.0, 150.0)

@patch('reconciler.StateMemory')
@patch('reconciler.HistoryLogger')
@patch('reconciler.TelegramNotifier')
def test_sync_data_removes_disappeared_position(mock_notifier_cls, mock_history_cls, mock_memory_cls, mock_broker):
    mock_broker.get_positions.return_value = [] 
    
    memory_instance = mock_memory_cls.return_value
    memory_instance.get_balance.return_value = 15000.0
    memory_instance.get_all_positions.return_value = pd.DataFrame([{
        'ticker': 'MSFT',
        'quantity': 2.0,
        'average_cost': 300.0,
        'entry_date': '2026-01-01'
    }])

    reconciler = Reconciler(broker=mock_broker)
    reconciler.sync_data()

    memory_instance.remove_position.assert_called_once_with('MSFT')
    mock_notifier_cls.return_value.send_message.assert_called()