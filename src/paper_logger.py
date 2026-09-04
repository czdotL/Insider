import threading
import json
import os
import re
import logging
import pandas as pd
import numpy as np
from datetime import datetime

import psycopg2
from sqlalchemy import create_engine

from analyzer import FundamentalAnalyzer
from config import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

paper_logger = logging.getLogger("PaperTrader")
paper_logger.setLevel(logging.INFO)
paper_logger.propagate = False

if not paper_logger.handlers:
    file_handler = logging.FileHandler(os.path.join(BASE_DIR, "paper.log"))
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    paper_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - [PAPER] - %(levelname)s - %(message)s'))
    paper_logger.addHandler(console_handler)


F_SCORE_THRESHOLD = 7           
STOP_LOSS_PCT = 0.35            
TAKE_PROFIT_PCT = 5.0           
MAX_HOLD_DAYS = 75              

LIVE_MIRROR_F_MIN = 4
LIVE_MIRROR_MIN_TRADE_VALUE = 300_000
LIVE_MIRROR_MIN_SHARE_PRICE = 5.0
LIVE_MIRROR_TITLES = ['VP', 'CFO', 'COB', 'Exec COB']
LIVE_MIRROR_REQUIRE_BULL = True
LIVE_MIRROR_BLOCK_Q1 = True     

VALUE_Q_F_MIN = 5
VALUE_Q_EV_EBITDA_MAX = 15.0

HYPER7_F_SCORE_MIN = 7              
HYPER7_EV_EBITDA_MAX = 25.0         
HYPER7_RET_180D_MIN = 0.0           
HYPER7_DRAWDOWN_FROM_PEAK_MAX = -0.10  
HYPER7_DRAWDOWN_FROM_PEAK_MIN = -0.50  
HYPER7_RET_5D_MIN = -0.10           
HYPER7_RET_5D_MAX = 0.03            
HYPER7_REQUIRE_BULL = True          

STOCK_MIN_PRICE = 1.0

PENNY_MIN_PRICE = 2.0
PENNY_MAX_PRICE = 5.0
PENNY_STOP_LOSS_PCT = 0.30          
PENNY_TAKE_PROFIT_PCT = 50.0        
PENNY_MAX_HOLD_DAYS = 5             
PENNY_MIN_DOLLAR_VOLUME = 100_000   

EXCLUDED_SECTORS = {
    'Financial Services',
    'Financial',
    'Banks',
    'Insurance',
    'REIT',
    'Real Estate',
}
EXCLUDED_INDUSTRY_SUBSTRINGS = [
    'Bank', 'REIT', 'Insurance', 'Capital Markets',
    'Mortgage', 'Credit Services',
]

STRATEGIES = [
    'strat_A_quality',
    'strat_B_live_mirror',
    'strat_C_baseline',
    'strat_D_value_quality',
    'strat_E_hyper7',
    'strat_F_penny',
]

MAX_HOLD_BY_STRATEGY = {
    'strat_F_penny': PENNY_MAX_HOLD_DAYS,
}
DEFAULT_MAX_HOLD_DAYS = MAX_HOLD_DAYS

HUF_USD = 380

PORTFOLIO_CONFIGS = [
    {'label': 'A_5pct',  'strategy': 'strat_A_quality',       'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.05},
    {'label': 'A_10pct', 'strategy': 'strat_A_quality',       'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.10},
    {'label': 'B_5pct',  'strategy': 'strat_B_live_mirror',   'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.05},
    {'label': 'B_10pct', 'strategy': 'strat_B_live_mirror',   'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.10},
    {'label': 'C_5pct',  'strategy': 'strat_C_baseline',      'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.05},
    {'label': 'C_10pct', 'strategy': 'strat_C_baseline',      'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.10},
    {'label': 'D_5pct',  'strategy': 'strat_D_value_quality', 'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.05},
    {'label': 'D_10pct', 'strategy': 'strat_D_value_quality', 'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.10},
    {'label': 'E_5pct',  'strategy': 'strat_E_hyper7',        'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.05},
    {'label': 'E_10pct', 'strategy': 'strat_E_hyper7',        'capital_usd': round(2_000_000 / HUF_USD), 'pos_pct': 0.10},
    {'label': 'F_5pct',  'strategy': 'strat_F_penny',         'capital_usd': round(1_000_000 / HUF_USD), 'pos_pct': 0.05},
    {'label': 'F_10pct', 'strategy': 'strat_F_penny',         'capital_usd': round(1_000_000 / HUF_USD), 'pos_pct': 0.10},
]


def yfinance_price_fetcher(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    import yfinance as yf
    prices = {}
    for tkr in tickers:
        try:
            hist = yf.Ticker(tkr).history(period='5d')
            if hist.empty or 'Close' not in hist.columns:
                continue
            close_series = hist['Close'].dropna()
            if close_series.empty:
                continue
            last_close = float(close_series.iloc[-1])
            if last_close > 0:
                prices[tkr] = last_close
        except Exception as e:
            paper_logger.error(f"{tkr}: yfinance price error: {e}", exc_info=True)
    return prices


def combined_price_fetcher(broker, tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    prices = {}
    if broker is not None:
        try:
            ibkr_prices = broker.get_live_prices(tickers) or {}
            prices.update(ibkr_prices)
        except Exception as e:
            paper_logger.error(f"IBKR price fetch error: {e}", exc_info=True)
    missing = [t for t in tickers if t not in prices]
    if missing:
        yf_prices = yfinance_price_fetcher(missing)
        prices.update(yf_prices)
    return prices


class PaperLogger:
    def __init__(self, db_url: str | None = None, broker=None) -> None:
        self.db_url = db_url or config.database_url
        self.broker = broker
        self._lock = threading.Lock()
        
        self.conn = psycopg2.connect(self.db_url)
        self.engine = create_engine(self.db_url)
        self.cursor = self.conn.cursor()
        
        self._create_tables()
        self.analyzer = FundamentalAnalyzer()

    def _create_tables(self) -> None:
        with self._lock:
            try:
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS signals (
                        id SERIAL PRIMARY KEY,
                        signal_date TEXT NOT NULL,
                        logged_at TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        insider_name TEXT,
                        title TEXT,
                        trade_value REAL,
                        insider_price REAL,
                        entry_price REAL,
                        f_score INTEGER,
                        f_score_status TEXT,
                        ev_ebitda REAL,
                        sector TEXT,
                        industry TEXT,
                        market_cap REAL,
                        is_financial INTEGER,
                        iv_at_signal REAL,
                        option_chain_snapshot_json TEXT,
                        market_regime TEXT,
                        avg_daily_volume REAL,
                        ret_180d REAL,
                        ret_5d REAL,
                        drawdown_from_90d_peak REAL,
                        strat_A_quality_decision TEXT,
                        strat_A_quality_reason TEXT,
                        strat_B_live_mirror_decision TEXT,
                        strat_B_live_mirror_reason TEXT,
                        strat_C_baseline_decision TEXT,
                        strat_C_baseline_reason TEXT,
                        strat_D_value_quality_decision TEXT,
                        strat_D_value_quality_reason TEXT,
                        strat_E_hyper7_decision TEXT,
                        strat_E_hyper7_reason TEXT,
                        strat_F_penny_decision TEXT,
                        strat_F_penny_reason TEXT,
                        analysis_json TEXT
                    )
                ''')
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS paper_positions (
                        id SERIAL PRIMARY KEY,
                        signal_id INTEGER NOT NULL,
                        strategy TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        entry_date TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        stop_loss REAL NOT NULL,
                        take_profit REAL NOT NULL,
                        exit_date TEXT,
                        exit_price REAL,
                        exit_reason TEXT,
                        pnl_pct REAL,
                        days_held INTEGER,
                        FOREIGN KEY (signal_id) REFERENCES signals(id)
                    )
                ''')
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS paper_portfolios (
                        label TEXT PRIMARY KEY,
                        strategy TEXT NOT NULL,
                        initial_capital REAL NOT NULL,
                        pos_pct REAL NOT NULL,
                        cash_balance REAL NOT NULL
                    )
                ''')
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS portfolio_allocations (
                        id SERIAL PRIMARY KEY,
                        portfolio_label TEXT NOT NULL,
                        position_id INTEGER NOT NULL,
                        signal_id INTEGER NOT NULL,
                        ticker TEXT NOT NULL,
                        action TEXT NOT NULL,
                        shares INTEGER,
                        entry_cost REAL,
                        FOREIGN KEY (portfolio_label) REFERENCES paper_portfolios(label),
                        FOREIGN KEY (position_id) REFERENCES paper_positions(id)
                    )
                ''')
                self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_pos_open ON paper_positions(exit_date, strategy)')
                self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_pos_ticker ON paper_positions(ticker, exit_date)')
                self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker, signal_date)')
                self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_alloc_pos ON portfolio_allocations(position_id)')
                self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_alloc_port ON portfolio_allocations(portfolio_label, action)')
                self.conn.commit()
            except Exception as e:
                paper_logger.error(f"Error creating paper tables: {e}", exc_info=True)
                self.conn.rollback()
                
        self._migrate()
        self._init_portfolios()

    def _migrate(self) -> None:
        ddls = [
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS market_regime TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS avg_daily_volume REAL",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS ret_180d REAL",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS ret_5d REAL",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS drawdown_from_90d_peak REAL",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_B_live_mirror_decision TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_B_live_mirror_reason TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_D_value_quality_decision TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_D_value_quality_reason TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_E_hyper7_decision TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_E_hyper7_reason TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_F_penny_decision TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS strat_F_penny_reason TEXT",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS bid_at_signal REAL",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS ask_at_signal REAL",
        ]
        with self._lock:
            for ddl in ddls:
                try:
                    self.cursor.execute(ddl)
                except Exception:
                    self.conn.rollback()
            self.conn.commit()

    def _init_portfolios(self) -> None:
        with self._lock:
            try:
                for cfg in PORTFOLIO_CONFIGS:
                    self.cursor.execute(
                        'SELECT label FROM paper_portfolios WHERE label = %s', (cfg['label'],))
                    if not self.cursor.fetchone():
                        self.cursor.execute('''
                            INSERT INTO paper_portfolios (label, strategy, initial_capital, pos_pct, cash_balance)
                            VALUES (%s, %s, %s, %s, %s)
                        ''', (cfg['label'], cfg['strategy'], cfg['capital_usd'],
                              cfg['pos_pct'], cfg['capital_usd']))
                self.conn.commit()
            except Exception as e:
                paper_logger.error(f"Error initializing portfolios: {e}", exc_info=True)
                self.conn.rollback()

    def _fetch_yfinance_info(self, ticker: str) -> dict:
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info
            return {
                'ev_ebitda': info.get('enterpriseToEbitda'),
                'sector': info.get('sector'),
                'industry': info.get('industry'),
                'market_cap': info.get('marketCap'),
                'bid': info.get('bid'),
                'ask': info.get('ask'),
            }
        except Exception as e:
            paper_logger.error(f"yfinance .info error for {ticker}: {e}", exc_info=True)
            return {'ev_ebitda': None, 'sector': None, 'industry': None,
                    'market_cap': None, 'bid': None, 'ask': None}

    @staticmethod
    def _extract_row_fields(row):
        iv = float(row['impliedVolatility']) if pd.notna(row.get('impliedVolatility')) else None
        bid = float(row['bid']) if pd.notna(row.get('bid')) else None
        ask = float(row['ask']) if pd.notna(row.get('ask')) else None
        last = float(row['lastPrice']) if pd.notna(row.get('lastPrice')) else None
        vol = int(row['volume']) if pd.notna(row.get('volume')) else 0
        oi = int(row['openInterest']) if pd.notna(row.get('openInterest')) else 0
        if iv is not None and iv < 0.05:
            iv = None
        return iv, bid, ask, last, vol, oi

    @staticmethod
    def _find_liquid_atm(calls_df, target_price: float, search_radius: int = 3):
        calls_df = calls_df.copy()
        calls_df['_dist'] = (calls_df['strike'] - target_price).abs()
        candidates = calls_df.nsmallest(search_radius * 2 + 1, '_dist')
        best_row = None
        best_score = -1

        for _, row in candidates.iterrows():
            bid_val = float(row['bid']) if pd.notna(row.get('bid')) else 0
            ask_val = float(row['ask']) if pd.notna(row.get('ask')) else 0
            oi_val = int(row['openInterest']) if pd.notna(row.get('openInterest')) else 0
            vol_val = int(row['volume']) if pd.notna(row.get('volume')) else 0
            iv_val = float(row['impliedVolatility']) if pd.notna(row.get('impliedVolatility')) else 0

            has_quote = bid_val > 0 or ask_val > 0
            has_valid_iv = 0.05 <= iv_val <= 5.0
            score = (oi_val * 2 + vol_val * 5
                     + (1000 if has_quote else 0)
                     + (500 if has_valid_iv else 0))

            if score > best_score:
                best_score = score
                best_row = row

        if best_row is None:
            best_row = candidates.iloc[0]
        return best_row

    def _fetch_option_snapshot(self, ticker: str):
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            expiries = t.options
            if not expiries or len(expiries) < 2:
                return None, None

            hist = t.history(period='5d')
            if hist.empty or 'Close' not in hist.columns:
                return None, None
            close_series = hist['Close'].dropna()
            if close_series.empty:
                return None, None
            current_price = float(close_series.iloc[-1])
            if current_price <= 0:
                return None, None

            today = datetime.now().date()
            candidate_expiries = []
            for exp_str in expiries:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    days = (exp_date - today).days
                    if 90 <= days <= 210:
                        candidate_expiries.append((exp_str, days))
                except ValueError:
                    continue

            if not candidate_expiries:
                for exp_str in expiries:
                    try:
                        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                        days = (exp_date - today).days
                        if days > 60:
                            candidate_expiries.append((exp_str, days))
                            if len(candidate_expiries) >= 3:
                                break
                    except ValueError:
                        continue

            if not candidate_expiries:
                return None, None

            best_expiry = None
            best_calls = None
            best_liquidity = -1
            best_dte = None

            for exp_str, dte in candidate_expiries:
                try:
                    chain = t.option_chain(exp_str)
                    calls = chain.calls
                    if calls.empty:
                        continue

                    calls_c = calls.copy()
                    calls_c['_dist'] = (calls_c['strike'] - current_price).abs()
                    atm_candidates = calls_c.nsmallest(3, '_dist')

                    total_oi = 0
                    total_vol = 0
                    has_any_quote = False
                    for _, row in atm_candidates.iterrows():
                        oi_val = int(row['openInterest']) if pd.notna(row.get('openInterest')) else 0
                        vol_val = int(row['volume']) if pd.notna(row.get('volume')) else 0
                        bid_val = float(row['bid']) if pd.notna(row.get('bid')) else 0
                        ask_val = float(row['ask']) if pd.notna(row.get('ask')) else 0
                        total_oi += oi_val
                        total_vol += vol_val
                        if bid_val > 0 or ask_val > 0:
                            has_any_quote = True

                    liquidity = total_oi * 2 + total_vol * 5 + (10000 if has_any_quote else 0)
                    if 150 <= dte <= 210:
                        liquidity += 5000

                    if liquidity > best_liquidity:
                        best_liquidity = liquidity
                        best_expiry = exp_str
                        best_calls = calls
                        best_dte = dte
                except Exception:
                    continue

            if best_expiry is None or best_calls is None:
                return None, None

            calls = best_calls
            atm_row = self._find_liquid_atm(calls, current_price)
            iv, bid, ask, last, vol, oi = self._extract_row_fields(atm_row)

            premium_est = None
            if bid and ask and bid > 0 and ask > 0:
                premium_est = (bid + ask) / 2
            elif last and last > 0:
                premium_est = last

            chain_total_oi = int(calls['openInterest'].fillna(0).sum())
            chain_total_volume = int(calls['volume'].fillna(0).sum())

            has_valid_iv = iv is not None and iv >= 0.05
            has_quote = (bid is not None and bid > 0) or (ask is not None and ask > 0)
            has_premium = premium_est is not None and premium_est > 0

            if has_valid_iv and has_quote:
                data_quality = 'good'
            elif has_valid_iv or has_premium:
                data_quality = 'partial'
            else:
                data_quality = 'no_data'

            snapshot = {
                'expiry': best_expiry,
                'dte': best_dte,
                'spot_at_signal': current_price,
                'atm_strike': float(atm_row['strike']),
                'atm_iv': iv,
                'atm_bid': bid,
                'atm_ask': ask,
                'atm_last': last,
                'atm_volume': vol,
                'atm_open_interest': oi,
                'atm_premium_est': premium_est,
                'chain_total_oi': chain_total_oi,
                'chain_total_volume': chain_total_volume,
                'data_quality': data_quality,
            }

            try:
                target_itm10 = current_price * 0.90
                itm10_row = self._find_liquid_atm(calls, target_itm10)
                itm10_strike_val = float(itm10_row['strike'])
                if itm10_strike_val < current_price:
                    iv10, bid10, ask10, last10, vol10, oi10 = self._extract_row_fields(itm10_row)
                    premium10 = None
                    if bid10 and ask10 and bid10 > 0 and ask10 > 0:
                        premium10 = (bid10 + ask10) / 2
                    elif last10 and last10 > 0:
                        premium10 = last10
                    snapshot['itm10_strike'] = itm10_strike_val
                    snapshot['itm10_iv'] = iv10
                    snapshot['itm10_bid'] = bid10
                    snapshot['itm10_ask'] = ask10
                    snapshot['itm10_last'] = last10
                    snapshot['itm10_volume'] = vol10
                    snapshot['itm10_open_interest'] = oi10
                    snapshot['itm10_premium_est'] = premium10
                    snapshot['itm10_moneyness_pct'] = (current_price - itm10_strike_val) / current_price
            except Exception:
                pass

            return iv, snapshot
        except Exception as e:
            paper_logger.error(f"Option snapshot error for {ticker}: {e}", exc_info=True)
            return None, None

    @staticmethod
    def _is_financial(sector: str, industry: str) -> bool:
        if sector and sector in EXCLUDED_SECTORS:
            return True
        if industry:
            for sub in EXCLUDED_INDUSTRY_SUBSTRINGS:
                if sub.lower() in industry.lower():
                    return True
        return False

    def _fetch_avg_volume(self, ticker: str) -> float | None:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period='1mo')
            if hist.empty or 'Volume' not in hist.columns:
                return None
            avg_vol = hist['Volume'].mean()
            return float(avg_vol) if avg_vol and avg_vol > 0 else None
        except Exception as e:
            paper_logger.error(f"Volume error for {ticker}: {e}", exc_info=True)
            return None

    def _fetch_price_pattern(self, ticker: str) -> dict | None:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period='1y')
            if hist.empty or 'Close' not in hist.columns or len(hist) < 30:
                return None

            close = hist['Close'].dropna()
            if len(close) < 30:
                return None

            current = float(close.iloc[-1])
            if current <= 0:
                return None

            ret_180d = None
            if len(close) >= 125:
                past_180 = float(close.iloc[-125])
                if past_180 > 0:
                    ret_180d = (current - past_180) / past_180

            ret_5d = None
            if len(close) >= 6:
                past_5 = float(close.iloc[-6])
                if past_5 > 0:
                    ret_5d = (current - past_5) / past_5

            drawdown = None
            window = close.iloc[-90:] if len(close) >= 90 else close
            peak = float(window.max())
            if peak > 0:
                drawdown = (current - peak) / peak

            return {
                'ret_180d': ret_180d,
                'ret_5d': ret_5d,
                'drawdown_from_90d_peak': drawdown,
            }
        except Exception as e:
            paper_logger.error(f"Price pattern error for {ticker}: {e}", exc_info=True)
            return None

    def _get_market_regime(self) -> str:
        try:
            import yfinance as yf
            spy = yf.Ticker('SPY').history(period='1y')
            if len(spy) < 200:
                return 'UNKNOWN'
            spy['MA200'] = spy['Close'].rolling(200).mean()
            last = spy.iloc[-2]
            if pd.isna(last['MA200']):
                return 'UNKNOWN'
            return 'BULL' if float(last['Close']) > float(last['MA200']) else 'BEAR'
        except Exception as e:
            paper_logger.error(f"Market regime error: {e}", exc_info=True)
            return 'UNKNOWN'

    @staticmethod
    def _decide_strat_A(is_financial: bool, f_score: int | None, f_status: str, share_price: float | None = None) -> tuple[str, str]:
        if is_financial:
            return 'EXCLUDED_FINANCIAL', 'Financial institution'
        if share_price is not None and share_price < STOCK_MIN_PRICE:
            return 'SKIP', f'Price ${share_price:.2f} < ${STOCK_MIN_PRICE}'
        if f_status != 'OK' or f_score is None:
            return 'SKIP', f'F-score cannot be calculated ({f_status})'
        if f_score >= F_SCORE_THRESHOLD:
            return 'BUY', f'F-score {f_score}/9 >= {F_SCORE_THRESHOLD}'
        return 'SKIP', f'F-score {f_score}/9 < {F_SCORE_THRESHOLD}'

    @staticmethod
    def _decide_strat_B(is_financial: bool, f_score: int | None, f_status: str,
                        title: str, trade_value: float | None, share_price: float | None, market_regime: str, signal_date: str) -> tuple[str, str]:
        if is_financial:
            return 'EXCLUDED_FINANCIAL', 'Financial institution'
        if not title:
            return 'SKIP', 'Missing title'
        title_str = str(title)
        if not any(re.search(r'\b' + re.escape(t) + r'\b', title_str, re.IGNORECASE) for t in LIVE_MIRROR_TITLES):
            return 'SKIP', f'Title does not match ({title})'
        if trade_value is None or trade_value < LIVE_MIRROR_MIN_TRADE_VALUE:
            return 'SKIP', f'Trade value ${trade_value or 0:,.0f} < ${LIVE_MIRROR_MIN_TRADE_VALUE:,.0f}'
        if share_price is None or share_price < LIVE_MIRROR_MIN_SHARE_PRICE:
            return 'SKIP', f'Share price ${share_price or 0:.2f} < ${LIVE_MIRROR_MIN_SHARE_PRICE}'
        if f_status != 'OK' or f_score is None:
            return 'SKIP', f'F-score cannot be calculated ({f_status})'
        if f_score < LIVE_MIRROR_F_MIN:
            return 'SKIP', f'F-score {f_score}/9 < {LIVE_MIRROR_F_MIN}'
        if LIVE_MIRROR_REQUIRE_BULL and market_regime != 'BULL':
            return 'SKIP', f'No bull regime (current: {market_regime})'
        if LIVE_MIRROR_BLOCK_Q1 and signal_date:
            try:
                month = int(str(signal_date)[5:7])
                if month in (1, 2, 3):
                    return 'SKIP', f'Q1 seasonal defense (month: {month})'
            except (ValueError, TypeError):
                pass
        return 'BUY', f'Live mirror criteria met'

    @staticmethod
    def _decide_strat_C(is_financial: bool) -> tuple[str, str]:
        if is_financial:
            return 'EXCLUDED_FINANCIAL', 'Financial institution'
        return 'BUY', 'Baseline: all signals'

    @staticmethod
    def _decide_strat_D(is_financial: bool, f_score: int | None, f_status: str, ev_ebitda: float | None, share_price: float | None = None) -> tuple[str, str]:
        if is_financial:
            return 'EXCLUDED_FINANCIAL', 'Financial institution'
        if share_price is not None and share_price < STOCK_MIN_PRICE:
            return 'SKIP', f'Price ${share_price:.2f} < ${STOCK_MIN_PRICE}'
        if f_status != 'OK' or f_score is None:
            return 'SKIP', f'F-score cannot be calculated ({f_status})'
        if f_score < VALUE_Q_F_MIN:
            return 'SKIP', f'F-score {f_score}/9 < {VALUE_Q_F_MIN}'
        if ev_ebitda is None:
            return 'SKIP', 'EV/EBITDA not available'
        if ev_ebitda <= 0:
            return 'SKIP', f'Negative EV/EBITDA ({ev_ebitda:.2f})'
        if ev_ebitda >= VALUE_Q_EV_EBITDA_MAX:
            return 'SKIP', f'EV/EBITDA {ev_ebitda:.2f} >= {VALUE_Q_EV_EBITDA_MAX}'
        return 'BUY', f'Value+quality criteria met'

    @staticmethod
    def _decide_strat_E(is_financial: bool, f_score: int | None, f_status: str, ev_ebitda: float | None,
                        market_regime: str, price_pattern: dict | None, share_price: float | None = None) -> tuple[str, str]:
        if is_financial:
            return 'EXCLUDED_FINANCIAL', 'Financial institution'
        if share_price is not None and share_price < STOCK_MIN_PRICE:
            return 'SKIP', f'Price ${share_price:.2f} < ${STOCK_MIN_PRICE}'
        if f_status != 'OK' or f_score is None:
            return 'SKIP', f'F-score cannot be calculated'
        if f_score < HYPER7_F_SCORE_MIN:
            return 'SKIP', f'F-score < {HYPER7_F_SCORE_MIN}'
        if ev_ebitda is None or ev_ebitda <= 0:
            return 'SKIP', 'EV/EBITDA invalid or missing'
        if ev_ebitda >= HYPER7_EV_EBITDA_MAX:
            return 'SKIP', f'EV/EBITDA >= {HYPER7_EV_EBITDA_MAX}'
        if HYPER7_REQUIRE_BULL and market_regime != 'BULL':
            return 'SKIP', 'No bull regime'
        if price_pattern is None:
            return 'SKIP', 'Price pattern data not available'

        ret_180 = price_pattern.get('ret_180d')
        ret_5 = price_pattern.get('ret_5d')
        dd = price_pattern.get('drawdown_from_90d_peak')

        if ret_180 is None or ret_5 is None or dd is None:
            return 'SKIP', 'Incomplete price pattern'
        if ret_180 < HYPER7_RET_180D_MIN:
            return 'SKIP', '180d return too low'
        if dd > HYPER7_DRAWDOWN_FROM_PEAK_MAX or dd < HYPER7_DRAWDOWN_FROM_PEAK_MIN:
            return 'SKIP', 'Drawdown out of range'
        if ret_5 < HYPER7_RET_5D_MIN or ret_5 > HYPER7_RET_5D_MAX:
            return 'SKIP', '5d return out of range'

        return 'BUY', 'HYPER7 criteria met'

    @staticmethod
    def _decide_strat_F(is_financial: bool, share_price: float | None, avg_volume: float | None, entry_price: float | None) -> tuple[str, str]:
        if is_financial:
            return 'EXCLUDED_FINANCIAL', 'Financial institution'
        if share_price is None or share_price < PENNY_MIN_PRICE:
            return 'SKIP', f'Price < ${PENNY_MIN_PRICE}'
        if share_price > PENNY_MAX_PRICE:
            return 'SKIP', f'Price > ${PENNY_MAX_PRICE}'
        dollar_vol = (avg_volume or 0) * (entry_price or share_price)
        if dollar_vol < PENNY_MIN_DOLLAR_VOLUME:
            return 'SKIP', 'Dollar volume too low'
        return 'BUY', 'Penny criteria met'

    def on_insider_signal(self, signal: dict) -> int | None:
        ticker = signal['ticker'].upper()
        logged_at = datetime.now().isoformat()

        with self._lock:
            try:
                self.cursor.execute('''
                    SELECT id FROM signals
                    WHERE ticker = %s AND CAST(signal_date AS DATE) >= CURRENT_DATE - INTERVAL '7 days'
                    LIMIT 1
                ''', (ticker,))
                if self.cursor.fetchone():
                    paper_logger.info(f"Ticker {ticker} already analyzed within the past 7 days — skipped.")
                    return None
            except Exception as e:
                paper_logger.error(f"Error checking recent signals: {e}", exc_info=True)
                self.conn.rollback()
                return None

        paper_logger.info(f"Processing paper signal for {ticker}...")

        analysis = self.analyzer.analyze(ticker, log_to_db=False)
        f_score = analysis.get('f_score')
        f_status = analysis.get('status', 'ERROR')

        info = self._fetch_yfinance_info(ticker)
        ev_ebitda = info['ev_ebitda']
        sector = info['sector']
        industry = info['industry']
        market_cap = info['market_cap']
        bid_at_signal = info.get('bid')
        ask_at_signal = info.get('ask')
        is_financial = self._is_financial(sector, industry)

        iv_at_signal, option_snapshot = self._fetch_option_snapshot(ticker)
        avg_volume = self._fetch_avg_volume(ticker)
        market_regime = self._get_market_regime()
        price_pattern = self._fetch_price_pattern(ticker)

        raw_price = signal.get('entry_price') or signal.get('insider_price')
        share_price = self._validate_entry_price(ticker, raw_price) if (raw_price and raw_price > 0) else raw_price

        a_dec, a_reason = self._decide_strat_A(is_financial, f_score, f_status, share_price)
        b_dec, b_reason = self._decide_strat_B(
            is_financial, f_score, f_status,
            signal.get('title'), signal.get('value'), share_price,
            market_regime, signal.get('signal_date'),
        )
        c_dec, c_reason = self._decide_strat_C(is_financial)
        d_dec, d_reason = self._decide_strat_D(is_financial, f_score, f_status, ev_ebitda, share_price)
        e_dec, e_reason = self._decide_strat_E(
            is_financial, f_score, f_status, ev_ebitda, market_regime, price_pattern, share_price
        )
        f_dec, f_reason = self._decide_strat_F(
            is_financial, share_price, avg_volume, share_price,
        )

        ret_180d_val = price_pattern.get('ret_180d') if price_pattern else None
        ret_5d_val = price_pattern.get('ret_5d') if price_pattern else None
        dd_val = price_pattern.get('drawdown_from_90d_peak') if price_pattern else None

        with self._lock:
            try:
                self.cursor.execute('''
                    INSERT INTO signals (
                        signal_date, logged_at, ticker, insider_name, title,
                        trade_value, insider_price, entry_price,
                        f_score, f_score_status, ev_ebitda,
                        sector, industry, market_cap, is_financial,
                        iv_at_signal, option_chain_snapshot_json,
                        market_regime, avg_daily_volume,
                        ret_180d, ret_5d, drawdown_from_90d_peak,
                        strat_A_quality_decision, strat_A_quality_reason,
                        strat_B_live_mirror_decision, strat_B_live_mirror_reason,
                        strat_C_baseline_decision, strat_C_baseline_reason,
                        strat_D_value_quality_decision, strat_D_value_quality_reason,
                        strat_E_hyper7_decision, strat_E_hyper7_reason,
                        strat_F_penny_decision, strat_F_penny_reason,
                        bid_at_signal, ask_at_signal,
                        analysis_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    signal.get('signal_date', datetime.now().strftime('%Y-%m-%d')),
                    logged_at, ticker,
                    signal.get('insider_name'), signal.get('title'),
                    signal.get('value'), signal.get('insider_price'), share_price,
                    f_score, f_status, ev_ebitda,
                    sector, industry, market_cap, 1 if is_financial else 0,
                    iv_at_signal,
                    json.dumps(option_snapshot) if option_snapshot else None,
                    market_regime, avg_volume,
                    ret_180d_val, ret_5d_val, dd_val,
                    a_dec, a_reason, b_dec, b_reason, c_dec, c_reason,
                    d_dec, d_reason, e_dec, e_reason,
                    f_dec, f_reason,
                    bid_at_signal, ask_at_signal,
                    json.dumps(analysis, default=str),
                ))
                
                signal_id = self.cursor.fetchone()[0]

                if share_price and share_price > 0:
                    for strat_name, dec in [
                        ('strat_A_quality', a_dec),
                        ('strat_B_live_mirror', b_dec),
                        ('strat_C_baseline', c_dec),
                        ('strat_D_value_quality', d_dec),
                        ('strat_E_hyper7', e_dec),
                        ('strat_F_penny', f_dec),
                    ]:
                        if dec == 'BUY':
                            if strat_name == 'strat_F_penny':
                                self._open_paper_position(signal_id, strat_name, ticker, share_price, signal.get('signal_date'),
                                                          sl_pct=PENNY_STOP_LOSS_PCT, tp_pct=PENNY_TAKE_PROFIT_PCT)
                            else:
                                self._open_paper_position(signal_id, strat_name, ticker, share_price, signal.get('signal_date'))

                self.conn.commit()
            except Exception as e:
                paper_logger.error(f"Database error saving signal: {e}", exc_info=True)
                self.conn.rollback()
                return None

        paper_logger.info(f"Analysis complete for {ticker} (ID: {signal_id})")
        return signal_id

    def _validate_entry_price(self, ticker: str, insider_price: float) -> float:
        market_price = None
        source = None

        if self.broker is not None:
            try:
                ibkr_prices = self.broker.get_live_prices([ticker]) or {}
                p = ibkr_prices.get(ticker)
                if p and p > 0:
                    market_price = p
                    source = 'IBKR'
            except Exception:
                pass

        if market_price is None:
            try:
                import yfinance as yf
                hist = yf.Ticker(ticker).history(period='5d')
                if not hist.empty and 'Close' in hist.columns:
                    close_series = hist['Close'].dropna()
                    if not close_series.empty:
                        last_close = float(close_series.iloc[-1])
                        if last_close > 0:
                            market_price = last_close
                            source = 'yfinance'
            except Exception:
                pass

        if market_price is None:
            return insider_price

        diff_pct = abs(insider_price - market_price) / market_price
        if diff_pct > 0.5:
            paper_logger.warning(f"{ticker}: Insider price ${insider_price:.2f} unrealistic "
                                 f"({source}: ${market_price:.2f}) -> using {source}.")
            return market_price
        return insider_price

    def _open_paper_position(self, signal_id: int, strategy: str, ticker: str, entry_price: float, entry_date: str,
                             sl_pct: float | None = None, tp_pct: float | None = None) -> None:
        entry_date = entry_date or datetime.now().strftime('%Y-%m-%d')
        sl = round(entry_price * (1 - (sl_pct or STOP_LOSS_PCT)), 4)
        tp = round(entry_price * (1 + (tp_pct or TAKE_PROFIT_PCT)), 4)

        try:
            self.cursor.execute('''
                INSERT INTO paper_positions (
                    signal_id, strategy, ticker, entry_date, entry_price,
                    stop_loss, take_profit, exit_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN')
                RETURNING id
            ''', (signal_id, strategy, ticker, entry_date, entry_price, sl, tp))
            
            position_id = self.cursor.fetchone()[0]

            for cfg in PORTFOLIO_CONFIGS:
                if cfg['strategy'] != strategy:
                    continue
                self.cursor.execute(
                    'SELECT cash_balance FROM paper_portfolios WHERE label = %s',
                    (cfg['label'],))
                row = self.cursor.fetchone()
                if not row:
                    continue
                cash = row[0]
                alloc = cfg['capital_usd'] * cfg['pos_pct']
                shares = int(alloc / entry_price) if entry_price > 0 else 0
                cost = shares * entry_price

                if shares >= 1 and cost <= cash:
                    self.cursor.execute('''
                        INSERT INTO portfolio_allocations
                            (portfolio_label, position_id, signal_id, ticker, action, shares, entry_cost)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''', (cfg['label'], position_id, signal_id, ticker, 'TAKEN', shares, cost))
                    self.cursor.execute(
                        'UPDATE paper_portfolios SET cash_balance = cash_balance - %s WHERE label = %s',
                        (cost, cfg['label']))
                else:
                    self.cursor.execute('''
                        INSERT INTO portfolio_allocations
                            (portfolio_label, position_id, signal_id, ticker, action, shares, entry_cost)
                        VALUES (%s, %s, %s, %s, %s, 0, 0)
                    ''', (cfg['label'], position_id, signal_id, ticker, 'SKIPPED_NO_CAPITAL'))
        except Exception as e:
            paper_logger.error(f"Error opening position: {e}", exc_info=True)
            self.conn.rollback()

    def update_open_positions_exits(self, price_fetcher) -> None:
        with self._lock:
            try:
                open_df = pd.read_sql_query(
                    "SELECT * FROM paper_positions WHERE exit_reason = 'OPEN'",
                    self.engine
                )
            except Exception as e:
                paper_logger.error(f"Error reading open positions: {e}")
                return

        if open_df.empty:
            paper_logger.info("Paper logger: No open positions to update.")
            return

        tickers = list(open_df['ticker'].unique())
        try:
            prices = price_fetcher(tickers) or {}
        except Exception as e:
            paper_logger.error(f"Paper logger price_fetcher error: {e}", exc_info=True)
            prices = {}

        today_np = np.datetime64(datetime.now().date(), 'D')
        closed_count = 0

        with self._lock:
            for _, row in open_df.iterrows():
                ticker = row['ticker']
                entry_price = row['entry_price']
                sl = row['stop_loss']
                tp = row['take_profit']
                entry_date = row['entry_date']

                try:
                    entry_np = np.datetime64(entry_date, 'D')
                    days_held = int(np.busday_count(entry_np, today_np))
                except Exception:
                    days_held = 0

                cur_price = prices.get(ticker)
                exit_reason = None
                exit_price = None

                if cur_price is not None and cur_price > 0:
                    if cur_price <= sl:
                        exit_reason = 'STOP_LOSS'
                        exit_price = cur_price
                    elif cur_price >= tp:
                        exit_reason = 'TAKE_PROFIT'
                        exit_price = tp

                strat_max_hold = MAX_HOLD_BY_STRATEGY.get(row['strategy'], DEFAULT_MAX_HOLD_DAYS)
                if exit_reason is None and days_held >= strat_max_hold:
                    exit_reason = 'MAX_HOLD'
                    if cur_price is not None and cur_price > 0:
                        exit_price = cur_price
                    else:
                        continue

                if exit_reason is None:
                    continue

                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                
                try:
                    self.cursor.execute('''
                        UPDATE paper_positions
                        SET exit_date = %s, exit_price = %s, exit_reason = %s,
                            pnl_pct = %s, days_held = %s
                        WHERE id = %s
                    ''', (
                        datetime.now().strftime('%Y-%m-%d'),
                        exit_price, exit_reason, pnl_pct, days_held, row['id']
                    ))
                    closed_count += 1

                    self.cursor.execute('''
                        SELECT portfolio_label, shares FROM portfolio_allocations
                        WHERE position_id = %s AND action = 'TAKEN'
                    ''', (row['id'],))
                    
                    for alloc_row in self.cursor.fetchall():
                        proceeds = alloc_row[1] * exit_price
                        self.cursor.execute(
                            'UPDATE paper_portfolios SET cash_balance = cash_balance + %s WHERE label = %s',
                            (proceeds, alloc_row[0]))
                except Exception as e:
                    paper_logger.error(f"Error updating exit for {ticker}: {e}", exc_info=True)
                    self.conn.rollback()
                    continue

            if closed_count > 0:
                self.conn.commit()

        paper_logger.info(f"Paper logger: {closed_count} position(s) closed.")

    def strategy_summary(self) -> pd.DataFrame:
        with self._lock:
            try:
                df = pd.read_sql_query("SELECT * FROM paper_positions", self.engine)
            except Exception as e:
                paper_logger.error(f"Error generating strategy summary: {e}")
                return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        summary_rows = []
        for strat in STRATEGIES:
            sub = df[df['strategy'] == strat]
            if sub.empty:
                continue
            closed = sub[sub['exit_reason'] != 'OPEN']
            opens = sub[sub['exit_reason'] == 'OPEN']

            wins = closed[closed['pnl_pct'] > 0]
            losses = closed[closed['pnl_pct'] <= 0]

            summary_rows.append({
                'strategy': strat,
                'total_entries': len(sub),
                'open': len(opens),
                'closed': len(closed),
                'wins': len(wins),
                'losses': len(losses),
                'win_rate_pct': (len(wins) / len(closed) * 100) if len(closed) > 0 else None,
                'avg_pnl_pct': closed['pnl_pct'].mean() if len(closed) > 0 else None,
                'median_pnl_pct': closed['pnl_pct'].median() if len(closed) > 0 else None,
                'best_pnl_pct': closed['pnl_pct'].max() if len(closed) > 0 else None,
                'worst_pnl_pct': closed['pnl_pct'].min() if len(closed) > 0 else None,
            })

        return pd.DataFrame(summary_rows)

    def get_signals_df(self, limit: int | None = None) -> pd.DataFrame:
        query = "SELECT * FROM signals ORDER BY logged_at DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        with self._lock:
            try:
                return pd.read_sql_query(query, self.engine)
            except Exception:
                return pd.DataFrame()

    def get_positions_df(self, strategy: str | None = None, only_closed: bool = False) -> pd.DataFrame:
        query = "SELECT * FROM paper_positions"
        conds = []
        if strategy:
            conds.append(f"strategy = '{strategy}'")
        if only_closed:
            conds.append("exit_reason != 'OPEN'")
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY entry_date DESC"
        
        with self._lock:
            try:
                return pd.read_sql_query(query, self.engine)
            except Exception:
                return pd.DataFrame()

    def portfolio_summary(self) -> pd.DataFrame:
        with self._lock:
            try:
                portfolios = pd.read_sql_query("SELECT * FROM paper_portfolios", self.engine)
                allocs = pd.read_sql_query(
                    "SELECT a.*, p.exit_price, p.pnl_pct, p.exit_reason "
                    "FROM portfolio_allocations a "
                    "LEFT JOIN paper_positions p ON a.position_id = p.id",
                    self.engine)
            except Exception:
                return pd.DataFrame()

        rows = []
        for _, pf in portfolios.iterrows():
            label = pf['label']
            sub = allocs[allocs['portfolio_label'] == label]
            taken = sub[sub['action'] == 'TAKEN']
            skipped = sub[sub['action'] == 'SKIPPED_NO_CAPITAL']
            closed = taken[taken['exit_reason'].notna() & (taken['exit_reason'] != 'OPEN')]
            still_open = taken[taken['exit_reason'].isna() | (taken['exit_reason'] == 'OPEN')]

            invested = still_open['entry_cost'].sum() if not still_open.empty else 0
            cash = pf['cash_balance']
            equity_est = cash + invested

            rows.append({
                'label': label,
                'strategy': pf['strategy'],
                'capital': pf['initial_capital'],
                'pos_pct': pf['pos_pct'],
                'cash': cash,
                'equity_est': equity_est,
                'pnl_pct': (equity_est / pf['initial_capital'] - 1) * 100 if pf['initial_capital'] > 0 else 0,
                'taken': len(taken),
                'skipped': len(skipped),
                'coverage': len(taken) / max(1, len(sub)) * 100 if len(sub) > 0 else 0,
                'closed': len(closed),
                'open': len(still_open),
                'avg_pnl': closed['pnl_pct'].mean() if not closed.empty else None,
            })
        return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    paper_log = PaperLogger()

    if len(sys.argv) < 2 or sys.argv[1] == 'summary':
        summary = paper_log.strategy_summary()
        if summary.empty:
            paper_logger.info("No paper positions yet.")
        else:
            print("Paper experiment summary:\n")
            print(summary.to_string(index=False))
        sys.exit(0)

    if sys.argv[1] == 'test':
        test_signal = {
            'ticker': sys.argv[2] if len(sys.argv) > 2 else 'AAPL',
            'insider_name': 'Test Insider',
            'title': 'CFO',
            'value': 500000,
            'insider_price': 100.0,
            'signal_date': datetime.now().strftime('%Y-%m-%d'),
            'entry_price': 100.0,
        }
        sig_id = paper_log.on_insider_signal(test_signal)
        paper_logger.info(f"Test signal executed (signal_id={sig_id}).")
        sys.exit(0)