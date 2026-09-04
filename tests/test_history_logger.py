# tests/test_history_logger.py

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch
from history_logger import HistoryLogger


@pytest.fixture
def history():
    """HistoryLogger with the Postgres driver mocked out, so tests exercise the
    class's own logic without needing a live database."""
    with patch('history_logger.psycopg2.connect') as mock_connect, \
         patch('history_logger.create_engine'), \
         patch('history_logger.plt.savefig'):
        mock_cursor = MagicMock()
        mock_connect.return_value.cursor.return_value = mock_cursor
        hl = HistoryLogger(db_url="postgresql://test:test@localhost/test")
        hl._mock_cursor = mock_cursor
        yield hl


def test_log_trade_execution(history):
    """Logging a fill issues an insert with the exact fill details, then commits."""
    history.log_trade(
        trade_date="2026-08-30",
        ticker="AAPL",
        action="BUY",
        quantity=5.0,
        fill_price=150.0,
        commission=1.0,
        order_id=999
    )

    sql, params = history._mock_cursor.execute.call_args[0]
    assert "INSERT INTO trades_log" in sql
    assert params == ("2026-08-30", "AAPL", "BUY", 5.0, 150.0, 1.0, 999)
    history.conn.commit.assert_called()


def test_generate_report_computes_return_and_drawdown(history):
    """Total return and max drawdown should be derived correctly from the equity curve,
    independent of the persistence layer."""
    equity_df = pd.DataFrame({
        'date': ['2026-08-01', '2026-08-02', '2026-08-03', '2026-08-04'],
        'cash_balance': [0, 0, 0, 0],
        'invested_value': [0, 0, 0, 0],
        'total_equity': [1000.0, 1200.0, 900.0, 1100.0],
    })
    trades_df = pd.DataFrame({'id': [1, 2], 'ticker': ['AAPL', 'AAPL']})

    history.get_equity_curve = MagicMock(return_value=equity_df)
    history.get_all_trades = MagicMock(return_value=trades_df)

    report = history.generate_report()

    assert report is not None
    assert report['trades'] == 2
    assert round(report['return'], 2) == 10.0          # (1100 - 1000) / 1000 * 100
    assert round(report['max_dd'], 2) == 25.0           # (900 - 1200) / 1200 * 100, abs()


def test_generate_report_returns_none_without_enough_history(history):
    """Fewer than two equity snapshots aren't enough to compute return/drawdown."""
    history.get_equity_curve = MagicMock(return_value=pd.DataFrame({'total_equity': [1000.0]}))
    history.get_all_trades = MagicMock(return_value=pd.DataFrame())

    assert history.generate_report() is None
