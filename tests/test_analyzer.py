# tests/test_analyzer.py

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch
from analyzer import FundamentalAnalyzer


@pytest.fixture
def analyzer():
    """FundamentalAnalyzer with the Postgres driver mocked out — these tests exercise the
    Piotroski F-Score scoring logic itself, not the persistence layer."""
    with patch('analyzer.psycopg2.connect') as mock_connect, \
         patch('analyzer.create_engine'):
        mock_connect.return_value.cursor.return_value = MagicMock()
        yield FundamentalAnalyzer(db_url="postgresql://test:test@localhost/test")


def _statement(rows: dict) -> pd.DataFrame:
    """Builds a 2-column (t, t-1) financial-statement DataFrame keyed by line-item label,
    matching the column ordering FundamentalAnalyzer._lookup expects (col 0 = current year)."""
    return pd.DataFrame({0: {k: v[0] for k, v in rows.items()},
                          1: {k: v[1] for k, v in rows.items()}})


def test_analyze_scores_a_healthy_company_as_buy(analyzer):
    income = _statement({
        'Net Income': (120.0, 100.0),
        'Total Revenue': (1000.0, 900.0),
        'Cost Of Revenue': (600.0, 580.0),
    })
    balance = _statement({
        'Total Assets': (2000.0, 1900.0),
        'Current Assets': (800.0, 700.0),
        'Current Liabilities': (300.0, 320.0),
        'Long Term Debt': (200.0, 250.0),
        'Ordinary Shares Number': (1000.0, 1000.0),
    })
    cashflow = _statement({'Operating Cash Flow': (150.0, 130.0)})

    analyzer._get_raw = MagicMock(return_value={'income': income, 'balance': balance, 'cashflow': cashflow})

    result = analyzer.analyze("TEST", log_to_db=False)

    assert result['status'] == 'OK'
    assert result['f_score'] == 9
    assert result['decision'] == 'BUY'


def test_analyze_flags_deteriorating_company_as_skip(analyzer):
    income = _statement({
        'Net Income': (-50.0, 100.0),
        'Total Revenue': (900.0, 1000.0),
        'Cost Of Revenue': (700.0, 600.0),
    })
    balance = _statement({
        'Total Assets': (1900.0, 2000.0),
        'Current Assets': (500.0, 800.0),
        'Current Liabilities': (400.0, 300.0),
        'Long Term Debt': (400.0, 200.0),
        'Ordinary Shares Number': (1200.0, 1000.0),
    })
    cashflow = _statement({'Operating Cash Flow': (-60.0, 130.0)})

    analyzer._get_raw = MagicMock(return_value={'income': income, 'balance': balance, 'cashflow': cashflow})

    result = analyzer.analyze("TEST", log_to_db=False)

    assert result['status'] == 'OK'
    assert result['f_score'] == 0
    assert result['decision'] == 'SKIP'


def test_analyze_reports_missing_annual_data(analyzer):
    analyzer._get_raw = MagicMock(return_value={
        'income': pd.DataFrame(), 'balance': pd.DataFrame(), 'cashflow': pd.DataFrame()
    })

    result = analyzer.analyze("TEST", log_to_db=False)

    assert result['status'] == 'NO_DATA'
    assert result['decision'] == 'SKIP'
