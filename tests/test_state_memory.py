import pytest
from unittest.mock import MagicMock, patch
from state_memory import StateMemory


@pytest.fixture
def memory():
    """StateMemory with the Postgres driver mocked out, so tests exercise the
    class's own logic (highest-price tracking, upsert parameters) without
    needing a live database."""
    with patch('state_memory.psycopg2.connect') as mock_connect, \
         patch('state_memory.create_engine'):
        mock_cursor = MagicMock()
        mock_connect.return_value.cursor.return_value = mock_cursor
        mem = StateMemory(db_url="postgresql://test:test@localhost/test")
        mem._mock_cursor = mock_cursor
        yield mem


def test_update_position_seeds_highest_price_on_new_entry(memory):
    """A brand-new position (no prior row) should seed highest_price from the entry cost."""
    memory._mock_cursor.fetchone.return_value = None

    memory.update_position("AAPL", 2.75, 150.0)

    sql, params = memory._mock_cursor.execute.call_args[0]
    assert "INSERT INTO positions" in sql
    ticker, qty, avg_cost, _entry_date, _last_updated, highest = params
    assert (ticker, qty, avg_cost, highest) == ("AAPL", 2.75, 150.0, 150.0)


def test_update_position_keeps_prior_highest_price(memory):
    """Re-entering at a lower average cost must not overwrite a higher previously recorded price."""
    memory._mock_cursor.fetchone.return_value = (180.0,)

    memory.update_position("AAPL", 5.5, 155.0)

    _sql, params = memory._mock_cursor.execute.call_args[0]
    *_, highest = params
    assert highest == 180.0  # max(155.0 new avg_cost, 180.0 existing_highest)


def test_pending_order_management(memory):
    """Adding and removing a pending order issues the expected upsert / delete statements."""
    memory.add_pending_order(order_id=1001, ticker="MSFT", action="BUY", qty=10.5, status="Submitted")
    insert_sql, insert_params = memory._mock_cursor.execute.call_args_list[-1][0]
    assert "INSERT INTO pending_orders" in insert_sql
    assert insert_params[:4] == (1001, "MSFT", "BUY", 10.5)

    memory.remove_pending_order(1001)
    delete_sql, delete_params = memory._mock_cursor.execute.call_args_list[-1][0]
    assert "DELETE FROM pending_orders" in delete_sql
    assert delete_params == (1001,)


def test_balance_updates(memory):
    """Writing a balance issues the expected upsert; reading returns whatever the DB reports."""
    memory.update_balance(8500.25)
    update_sql, update_params = memory._mock_cursor.execute.call_args[0]
    assert "INSERT INTO balance" in update_sql
    assert update_params[0] == 8500.25

    memory._mock_cursor.fetchone.return_value = (8500.25,)
    assert memory.get_balance() == 8500.25
