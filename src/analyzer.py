"""
Piotroski F-Score Fundamental Analyzer (PostgreSQL)
===================================================
"""

import os
import sys
import threading
import json
import logging
from io import StringIO
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd
import psycopg2
from sqlalchemy import create_engine

from config import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# DEDICATED ANALYZER LOGGER SETUP
# ============================================================
analyzer_logger = logging.getLogger("FundamentalAnalyzer")
analyzer_logger.setLevel(logging.INFO)
analyzer_logger.propagate = False

if not analyzer_logger.handlers:
    file_handler = logging.FileHandler(os.path.join(BASE_DIR, "analyzer.log"))
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    analyzer_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - [ANALYZER] - %(levelname)s - %(message)s'))
    analyzer_logger.addHandler(console_handler)

# ============================================================
# RULEBOOK
# ============================================================
F_SCORE_BUY_THRESHOLD = 7
CACHE_TTL_HOURS = 24

FIELD_CANDIDATES = {
    'net_income': [
        'Net Income',
        'Net Income Common Stockholders',
        'Net Income From Continuing Operation Net Minority Interest',
    ],
    'total_revenue': [
        'Total Revenue',
        'Revenue',
        'Operating Revenue',
    ],
    'cogs': [
        'Cost Of Revenue',
        'Cost Of Goods Sold',
        'Reconciled Cost Of Revenue',
    ],
    'total_assets': [
        'Total Assets',
    ],
    'current_assets': [
        'Current Assets',
        'Total Current Assets',
    ],
    'current_liabilities': [
        'Current Liabilities',
        'Total Current Liabilities',
    ],
    'long_term_debt': [
        'Long Term Debt',
        'Long Term Debt And Capital Lease Obligation',
    ],
    'ocf': [
        'Operating Cash Flow',
        'Cash Flow From Continuing Operating Activities',
        'Total Cash From Operating Activities',
    ],
    'shares': [
        'Ordinary Shares Number',
        'Share Issued',
        'Common Stock Shares Issued',
    ],
}


class FundamentalAnalyzer:
    def __init__(self, db_url: str | None = None) -> None:
        self.db_url = db_url or config.database_url
        self._lock = threading.Lock()
        
        self.conn = psycopg2.connect(self.db_url)
        self.engine = create_engine(self.db_url)
        self.cursor = self.conn.cursor()
        
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            try:
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS ticker_raw_cache (
                        ticker TEXT PRIMARY KEY,
                        fetched_at TEXT NOT NULL,
                        income_json TEXT,
                        balance_json TEXT,
                        cashflow_json TEXT
                    )
                ''')
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS analysis_log (
                        id SERIAL PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        analyzed_at TEXT NOT NULL,
                        f_score INTEGER,
                        decision TEXT,
                        status TEXT,
                        result_json TEXT NOT NULL
                    )
                ''')
                self.conn.commit()
            except Exception as e:
                analyzer_logger.error(f"Error creating analyzer tables: {e}", exc_info=True)
                self.conn.rollback()

    def _load_cached(self, ticker: str):
        with self._lock:
            try:
                self.cursor.execute('''
                    SELECT fetched_at, income_json, balance_json, cashflow_json
                    FROM ticker_raw_cache WHERE ticker = %s
                ''', (ticker,))
                row = self.cursor.fetchone()
            except Exception as e:
                analyzer_logger.error(f"Error loading cache for {ticker}: {e}", exc_info=True)
                self.conn.rollback()
                return None

        if not row:
            return None
            
        try:
            fetched_at = datetime.fromisoformat(row[0])
        except ValueError:
            return None
            
        if datetime.now() - fetched_at > timedelta(hours=CACHE_TTL_HOURS):
            return None
            
        return {
            'income': self._json_to_df(row[1]),
            'balance': self._json_to_df(row[2]),
            'cashflow': self._json_to_df(row[3]),
        }

    def _save_cached(self, ticker: str, income, balance, cashflow) -> None:
        now_iso = datetime.now().isoformat()
        with self._lock:
            try:
                self.cursor.execute('''
                    INSERT INTO ticker_raw_cache
                    (ticker, fetched_at, income_json, balance_json, cashflow_json)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(ticker) DO UPDATE SET
                        fetched_at=EXCLUDED.fetched_at,
                        income_json=EXCLUDED.income_json,
                        balance_json=EXCLUDED.balance_json,
                        cashflow_json=EXCLUDED.cashflow_json
                ''', (
                    ticker, now_iso,
                    self._df_to_json(income),
                    self._df_to_json(balance),
                    self._df_to_json(cashflow),
                ))
                self.conn.commit()
            except Exception as e:
                analyzer_logger.error(f"Error saving cache for {ticker}: {e}", exc_info=True)
                self.conn.rollback()

    @staticmethod
    def _df_to_json(df) -> str | None:
        if df is None or df.empty:
            return None
        try:
            return df.to_json(orient='split', date_format='iso')
        except Exception:
            return None

    @staticmethod
    def _json_to_df(json_str: str) -> pd.DataFrame | None:
        if not json_str:
            return None
        try:
            return pd.read_json(StringIO(json_str), orient='split')
        except Exception:
            return None

    def _fetch_fresh(self, ticker: str) -> dict:
        try:
            t = yf.Ticker(ticker)
            income = t.financials
            balance = t.balance_sheet
            cashflow = t.cashflow

            if income.empty and balance.empty and cashflow.empty:
                return {'error': 'yfinance returned empty DataFrames.'}

            self._save_cached(ticker, income, balance, cashflow)
            return {'income': income, 'balance': balance, 'cashflow': cashflow}
        except Exception as e:
            return {'error': f"{type(e).__name__}: {e}"}

    def _get_raw(self, ticker: str) -> dict:
        cached = self._load_cached(ticker)
        if cached is not None:
            return cached
        return self._fetch_fresh(ticker)

    @staticmethod
    def _lookup(df, logical_key: str, col_idx: int = 0):
        if df is None or df.empty:
            return None, None
        if col_idx >= df.shape[1]:
            return None, None

        for field in FIELD_CANDIDATES.get(logical_key, []):
            if field in df.index:
                try:
                    val = df.iloc[df.index.get_loc(field), col_idx]
                    if pd.notna(val):
                        return float(val), field
                except (IndexError, KeyError, ValueError, TypeError):
                    continue
        return None, None

    def _compute_checks(self, income, balance, cashflow):
        def L(df, key, i):
            v, _ = self._lookup(df, key, i)
            return v

        net_income_t = L(income, 'net_income', 0)
        net_income_tm1 = L(income, 'net_income', 1)
        revenue_t = L(income, 'total_revenue', 0)
        revenue_tm1 = L(income, 'total_revenue', 1)
        cogs_t = L(income, 'cogs', 0)
        cogs_tm1 = L(income, 'cogs', 1)

        assets_t = L(balance, 'total_assets', 0)
        assets_tm1 = L(balance, 'total_assets', 1)
        curr_assets_t = L(balance, 'current_assets', 0)
        curr_assets_tm1 = L(balance, 'current_assets', 1)
        curr_liab_t = L(balance, 'current_liabilities', 0)
        curr_liab_tm1 = L(balance, 'current_liabilities', 1)
        lt_debt_t = L(balance, 'long_term_debt', 0)
        lt_debt_tm1 = L(balance, 'long_term_debt', 1)
        shares_t = L(balance, 'shares', 0)
        shares_tm1 = L(balance, 'shares', 1)

        ocf_t = L(cashflow, 'ocf', 0)

        raw_metrics = {
            'net_income_t': net_income_t, 'net_income_tm1': net_income_tm1,
            'revenue_t': revenue_t, 'revenue_tm1': revenue_tm1,
            'cogs_t': cogs_t, 'cogs_tm1': cogs_tm1,
            'assets_t': assets_t, 'assets_tm1': assets_tm1,
            'curr_assets_t': curr_assets_t, 'curr_assets_tm1': curr_assets_tm1,
            'curr_liab_t': curr_liab_t, 'curr_liab_tm1': curr_liab_tm1,
            'lt_debt_t': lt_debt_t, 'lt_debt_tm1': lt_debt_tm1,
            'shares_t': shares_t, 'shares_tm1': shares_tm1,
            'ocf_t': ocf_t,
        }

        checks = {}

        if net_income_t is None:
            checks['1_net_income_positive'] = {'pass': False, 'value': None, 'reason': 'Missing Net Income'}
        else:
            checks['1_net_income_positive'] = {
                'pass': net_income_t > 0,
                'value': net_income_t,
                'reason': f"Net Income = {net_income_t:,.0f}"
            }

        if ocf_t is None:
            checks['2_ocf_positive'] = {'pass': False, 'value': None, 'reason': 'Missing OCF'}
        else:
            checks['2_ocf_positive'] = {
                'pass': ocf_t > 0,
                'value': ocf_t,
                'reason': f"OCF = {ocf_t:,.0f}"
            }

        if None in (net_income_t, net_income_tm1, assets_t, assets_tm1) or not assets_t or not assets_tm1:
            checks['3_roa_improved'] = {'pass': False, 'value': None, 'reason': 'Missing ROA data'}
        else:
            roa_t = net_income_t / assets_t
            roa_tm1 = net_income_tm1 / assets_tm1
            checks['3_roa_improved'] = {
                'pass': roa_t > roa_tm1,
                'value': {'t': roa_t, 't-1': roa_tm1},
                'reason': f"ROA {roa_tm1:.2%} → {roa_t:.2%}"
            }

        if None in (ocf_t, net_income_t):
            checks['4_quality_of_earnings'] = {'pass': False, 'value': None, 'reason': 'Missing data'}
        else:
            checks['4_quality_of_earnings'] = {
                'pass': ocf_t > net_income_t,
                'value': {'ocf': ocf_t, 'ni': net_income_t},
                'reason': f"OCF ({ocf_t:,.0f}) {'>' if ocf_t > net_income_t else '≤'} NI ({net_income_t:,.0f})"
            }

        if lt_debt_t is None and lt_debt_tm1 is None:
            checks['5_leverage_decreased'] = {
                'pass': True,
                'value': {'t': 0, 't-1': 0},
                'reason': 'No LT debt in either year (debt-free)'
            }
        elif None in (lt_debt_t, lt_debt_tm1, assets_t, assets_tm1) or not assets_t or not assets_tm1:
            checks['5_leverage_decreased'] = {'pass': False, 'value': None, 'reason': 'Missing / inconsistent LT debt data'}
        else:
            ratio_t = lt_debt_t / assets_t
            ratio_tm1 = lt_debt_tm1 / assets_tm1
            checks['5_leverage_decreased'] = {
                'pass': ratio_t < ratio_tm1,
                'value': {'t': ratio_t, 't-1': ratio_tm1},
                'reason': f"LT debt/Assets {ratio_tm1:.2%} → {ratio_t:.2%}"
            }

        if None in (curr_assets_t, curr_assets_tm1, curr_liab_t, curr_liab_tm1) or not curr_liab_t or not curr_liab_tm1:
            checks['6_current_ratio_improved'] = {'pass': False, 'value': None, 'reason': 'Missing Current ratio data'}
        else:
            cr_t = curr_assets_t / curr_liab_t
            cr_tm1 = curr_assets_tm1 / curr_liab_tm1
            checks['6_current_ratio_improved'] = {
                'pass': cr_t > cr_tm1,
                'value': {'t': cr_t, 't-1': cr_tm1},
                'reason': f"Current ratio {cr_tm1:.2f} → {cr_t:.2f}"
            }

        if None in (shares_t, shares_tm1) or not shares_tm1:
            checks['7_no_dilution'] = {'pass': False, 'value': None, 'reason': 'Missing shares data'}
        else:
            diff_pct = (shares_t - shares_tm1) / shares_tm1
            checks['7_no_dilution'] = {
                'pass': diff_pct <= 0.001,
                'value': {'t': shares_t, 't-1': shares_tm1, 'diff_pct': diff_pct},
                'reason': f"Shares: {shares_tm1:,.0f} → {shares_t:,.0f} ({diff_pct:+.2%})"
            }

        if None in (revenue_t, revenue_tm1, cogs_t, cogs_tm1) or not revenue_t or not revenue_tm1:
            checks['8_gross_margin_improved'] = {'pass': False, 'value': None, 'reason': 'Missing gross margin data'}
        else:
            gm_t = (revenue_t - cogs_t) / revenue_t
            gm_tm1 = (revenue_tm1 - cogs_tm1) / revenue_tm1
            checks['8_gross_margin_improved'] = {
                'pass': gm_t > gm_tm1,
                'value': {'t': gm_t, 't-1': gm_tm1},
                'reason': f"Gross margin {gm_tm1:.2%} → {gm_t:.2%}"
            }

        if None in (revenue_t, revenue_tm1, assets_t, assets_tm1) or not assets_t or not assets_tm1:
            checks['9_asset_turnover_improved'] = {'pass': False, 'value': None, 'reason': 'Missing asset turnover data'}
        else:
            at_t = revenue_t / assets_t
            at_tm1 = revenue_tm1 / assets_tm1
            checks['9_asset_turnover_improved'] = {
                'pass': at_t > at_tm1,
                'value': {'t': at_t, 't-1': at_tm1},
                'reason': f"Asset turnover {at_tm1:.3f} → {at_t:.3f}"
            }

        return checks, raw_metrics

    def analyze(self, ticker: str, log_to_db: bool = True) -> dict:
        ticker = ticker.upper()
        result = {
            'ticker': ticker,
            'timestamp': datetime.now().isoformat(),
            'status': 'OK',
            'f_score': None,
            'decision': 'SKIP',
            'checks': {},
            'raw_metrics': {},
            'warnings': []
        }

        raw = self._get_raw(ticker)

        if 'error' in raw:
            result['status'] = 'ERROR'
            result['warnings'].append(f"Data fetch error: {raw['error']}")
            analyzer_logger.error(f"Data fetch error for {ticker}: {raw['error']}")
            if log_to_db:
                self._log_analysis(result)
            return result

        income = raw.get('income')
        balance = raw.get('balance')
        cashflow = raw.get('cashflow')

        missing = []
        for name, df in [('income', income), ('balance', balance), ('cashflow', cashflow)]:
            if df is None or df.empty:
                missing.append(f"{name}: empty")
            elif df.shape[1] < 2:
                missing.append(f"{name}: only {df.shape[1]} year(s)")

        if missing:
            result['status'] = 'NO_DATA'
            result['warnings'].append('Insufficient annual data: ' + '; '.join(missing))
            if log_to_db:
                self._log_analysis(result)
            return result

        checks, raw_metrics = self._compute_checks(income, balance, cashflow)
        f_score = sum(1 for c in checks.values() if c['pass'] is True)

        unknowns = sum(1 for c in checks.values() if c['value'] is None)
        if unknowns >= 3:
            result['warnings'].append(f'{unknowns}/9 checks cannot be calculated — F-score is uncertain.')

        result['checks'] = checks
        result['raw_metrics'] = raw_metrics
        result['f_score'] = f_score
        result['decision'] = 'BUY' if f_score >= F_SCORE_BUY_THRESHOLD else 'SKIP'

        if log_to_db:
            self._log_analysis(result)
        return result

    def _log_analysis(self, result: dict) -> None:
        try:
            with self._lock:
                self.cursor.execute('''
                    INSERT INTO analysis_log
                    (ticker, analyzed_at, f_score, decision, status, result_json)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (
                    result['ticker'],
                    result['timestamp'],
                    result['f_score'],
                    result['decision'],
                    result['status'],
                    json.dumps(result, default=str),
                ))
                self.conn.commit()
        except Exception as e:
            analyzer_logger.error(f"Error logging analysis result: {e}", exc_info=True)
            self.conn.rollback()

    def get_analysis_log(self, limit: int = 500) -> pd.DataFrame:
        with self._lock:
            try:
                return pd.read_sql_query(
                    f"SELECT * FROM analysis_log ORDER BY analyzed_at DESC LIMIT {int(limit)}",
                    self.engine
                )
            except Exception as e:
                analyzer_logger.error(f"Error fetching analysis log: {e}", exc_info=True)
                return pd.DataFrame()

    def pretty_print(self, result: dict) -> None:
        print("=" * 64)
        print(f"Piotroski F-Score Analysis for {result['ticker']}")
        print(f"Timestamp: {result['timestamp'][:19]}")
        print(f"Status: {result['status']}")
        print("=" * 64)

        if result['status'] != 'OK':
            for w in result['warnings']:
                print(f"   [WARNING] {w}")
            print("=" * 64)
            return

        for check_id in sorted(result['checks'].keys()):
            check = result['checks'][check_id]
            if check['pass'] is True:
                status_label = '[PASS]'
            elif check['value'] is None:
                status_label = '[UNKNOWN]'
            else:
                status_label = '[FAIL]'
            
            num, _, rest = check_id.partition('_')
            name = rest.replace('_', ' ').title()
            print(f"   {status_label} {num}. {name}: {check['reason']}")

        print("-" * 64)
        print(f"   F-SCORE: {result['f_score']}/9   (Threshold for BUY: ≥{F_SCORE_BUY_THRESHOLD})")
        print(f"   DECISION: {result['decision']}")

        for w in result['warnings']:
            print(f"   [WARNING] {w}")
        print("=" * 64)


def analyze_ticker(ticker: str, db_url: str | None = None) -> dict:
    analyzer = FundamentalAnalyzer(db_url=db_url)
    return analyzer.analyze(ticker)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyzer.py TICKER [TICKER ...]")
        print("Example: python analyzer.py AAPL MSFT HOMB")
        sys.exit(1)

    analyzer = FundamentalAnalyzer()
    for ticker in sys.argv[1:]:
        try:
            result = analyzer.analyze(ticker)
            analyzer.pretty_print(result)
            print()
        except Exception as e:
            analyzer_logger.error(f"Analysis failed for {ticker}: {e}", exc_info=True)
            print(f"Analysis failed for {ticker}: {e}\n")