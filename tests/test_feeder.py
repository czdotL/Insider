# tests/test_feeder.py

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from feeder import OpenInsiderFeeder

@patch('os.path.exists', return_value=False)
@patch('feeder.requests.get')
@patch('feeder.TelegramNotifier')
def test_fetch_insider_trades_success(mock_notifier_cls, mock_get, mock_exists):
    """Test that valid HTML tables from OpenInsider are parsed correctly into a DataFrame."""
    mock_html = """
    <html>
        <body>
            <table class="tinytable">
                <tr>
                    <th>X</th><th>Filing Date</th><th>Trade Date</th><th>Ticker</th><th>Company Name</th>
                    <th>Insider Name</th><th>Title</th><th>Trade Type</th><th>Price</th><th>Qty</th>
                    <th>Owned</th><th>ΔOwn</th><th>Value</th><th>1d%</th><th>1w%</th><th>1m%</th><th>6m%</th>
                </tr>
                <tr>
                    <td></td>
                    <td>2026-08-30 10:00:00</td>
                    <td>2026-08-29</td>
                    <td>AAPL</td>
                    <td>Apple Inc</td>
                    <td>Tim Cook</td>
                    <td>CEO</td>
                    <td>P - Purchase</td>
                    <td>$150.00</td>
                    <td>10,000</td>
                    <td>1,000,000</td>
                    <td>+1%</td>
                    <td>$1,500,000</td>
                    <td>0%</td><td>0%</td><td>0%</td><td>0%</td>
                </tr>
            </table>
        </body>
    </html>
    """
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = mock_html
    mock_get.return_value = mock_response

    feeder = OpenInsiderFeeder()
    df = feeder.get_latest_buys()

    assert not df.empty
    assert df.iloc[0]['ticker'] == "AAPL"
    assert df.iloc[0]['title'] == "CEO"
    assert df.iloc[0]['price'] == 150.00

@patch('os.path.exists', return_value=False)
@patch('feeder.requests.get')
@patch('feeder.TelegramNotifier')
def test_fetch_insider_trades_http_error(mock_notifier_cls, mock_get, mock_exists):
    """Test that the feeder handles server downtime gracefully without raising exceptions."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_get.return_value = mock_response

    feeder = OpenInsiderFeeder()
    df = feeder.get_latest_buys()

    assert df.empty