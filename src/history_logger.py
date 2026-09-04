# Beépített (Standard library) modulok
import os
import threading
from datetime import datetime
import psycopg2
from sqlalchemy import create_engine
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from logger import logger
from config import config


class HistoryLogger:
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
                    CREATE TABLE IF NOT EXISTS trades_log (
                        id SERIAL PRIMARY KEY,
                        trade_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        action TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        fill_price REAL NOT NULL,
                        commission REAL NOT NULL,
                        order_id INTEGER
                    )
                ''')
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS daily_equity (
                        date TEXT PRIMARY KEY,
                        cash_balance REAL NOT NULL,
                        invested_value REAL NOT NULL,
                        total_equity REAL NOT NULL
                    )
                ''')
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error creating history tables: {e}", exc_info=True)
                self.conn.rollback()

    def log_trade(self, trade_date: str, ticker: str, action: str, quantity: float, fill_price: float, commission: float, order_id: int | None = None) -> None:
        with self._lock:
            try:
                self.cursor.execute('''
                    INSERT INTO trades_log (trade_date, ticker, action, quantity, fill_price, commission, order_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', (trade_date, ticker, action, quantity, fill_price, commission, order_id))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error logging trade for {ticker}: {e}", exc_info=True)
                self.conn.rollback()

    def get_all_trades(self) -> pd.DataFrame:
        with self._lock:
            try:
                return pd.read_sql_query("SELECT * FROM trades_log", self.engine)
            except Exception as e:
                logger.error(f"Error fetching all trades: {e}", exc_info=True)
                return pd.DataFrame()

    def log_daily_equity(self, cash: float, invested: float, total: float) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            try:
                self.cursor.execute('''
                    INSERT INTO daily_equity (date, cash_balance, invested_value, total_equity)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(date) DO UPDATE SET
                        cash_balance=EXCLUDED.cash_balance,
                        invested_value=EXCLUDED.invested_value,
                        total_equity=EXCLUDED.total_equity
                ''', (today, float(cash), float(invested), float(total)))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error logging daily equity: {e}", exc_info=True)
                self.conn.rollback()

    def get_equity_curve(self) -> pd.DataFrame:
        with self._lock:
            try:
                return pd.read_sql_query("SELECT * FROM daily_equity ORDER BY date ASC", self.engine)
            except Exception as e:
                logger.error(f"Error fetching equity curve: {e}", exc_info=True)
                return pd.DataFrame()

    def generate_report(self) -> dict[str, float | int] | None:
        """Generates performance statistics and the equity curve chart."""
        try:
            equity_df = self.get_equity_curve()
            trades_df = self.get_all_trades()

            if equity_df.empty or len(equity_df) < 2:
                return None

            start_equity = equity_df['total_equity'].iloc[0]
            final_equity = equity_df['total_equity'].iloc[-1]
            num_trades = len(trades_df)

            total_return_pct = ((final_equity - start_equity) / start_equity) * 100 if start_equity > 0 else 0

            rolling_max = equity_df['total_equity'].cummax()
            drawdown = (equity_df['total_equity'] - rolling_max) / rolling_max
            max_dd_pct = drawdown.min() * 100

            equity_df['daily_return'] = equity_df['total_equity'].pct_change()
            mean_return = equity_df['daily_return'].mean()
            std_return = equity_df['daily_return'].std()

            if std_return > 0 and not np.isnan(std_return):
                sharpe_ratio = (mean_return / std_return) * np.sqrt(252)
            else:
                sharpe_ratio = 0.0

            # Chart generation
            plt.figure(figsize=(10, 5))
            plt.plot(equity_df['date'], equity_df['total_equity'], label='Total Equity ($)', color='#007ACC', linewidth=2)
            plt.fill_between(equity_df['date'], equity_df['total_equity'],
                             equity_df['total_equity'].min() * 0.98, color='#007ACC', alpha=0.1)
            plt.title('Equity Curve', fontsize=14, fontweight='bold')
            plt.xlabel('Date')
            plt.ylabel('Equity (USD)')
            plt.grid(True, linestyle='--', alpha=0.6)
            plt.legend()
            plt.gcf().autofmt_xdate()
            plt.tight_layout()
            plt.savefig('equity_curve.png', dpi=150)
            plt.close('all')

            return {
                'return': total_return_pct,
                'sharpe': sharpe_ratio,
                'max_dd': abs(max_dd_pct),
                'trades': num_trades
            }
        except Exception as e:
            logger.error(f"Error generating report: {e}", exc_info=True)
            return None