import os
import threading
from datetime import datetime

import psycopg2
from sqlalchemy import create_engine
import pandas as pd

from logger import logger
from config import config

class StateMemory:
    def __init__(self, db_url: str | None = None):
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
                    CREATE TABLE IF NOT EXISTS positions (
                        ticker TEXT PRIMARY KEY,
                        quantity REAL NOT NULL,
                        average_cost REAL NOT NULL,
                        entry_date TEXT NOT NULL,
                        last_updated TEXT NOT NULL,
                        highest_price REAL
                    )
                ''')
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pending_orders (
                        order_id INTEGER PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        action TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        status TEXT NOT NULL,
                        submitted_at TEXT NOT NULL
                    )
                ''')
                self.cursor.execute('''
                    CREATE TABLE IF NOT EXISTS balance (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        cash_balance REAL NOT NULL,
                        last_updated TEXT NOT NULL
                    )
                ''')
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error creating state memory tables: {e}", exc_info=True)
                self.conn.rollback()
        self._migrate()

    def _migrate(self) -> None:
        """Schema updates for existing databases."""
        with self._lock:
            try:
                self.cursor.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS highest_price REAL")
                self.cursor.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS sl_price REAL")
                self.cursor.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS tp_price REAL")
                
                self.cursor.execute('''
                    UPDATE positions 
                    SET highest_price = average_cost 
                    WHERE highest_price IS NULL
                ''')
                self.conn.commit()
            except Exception as e:
                logger.error(f"Migration error: {e}", exc_info=True)
                self.conn.rollback()

    # --- POSITIONS ---

    def update_position(self, ticker: str, qty: float, avg_cost: float) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            try:
                self.cursor.execute("SELECT highest_price FROM positions WHERE ticker = %s", (ticker,))
                row = self.cursor.fetchone()
                existing_highest = row[0] if row and row[0] else 0

                self.cursor.execute('''
                    INSERT INTO positions (ticker, quantity, average_cost, entry_date, last_updated, highest_price)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(ticker) DO UPDATE SET
                        quantity=EXCLUDED.quantity,
                        average_cost=EXCLUDED.average_cost,
                        last_updated=EXCLUDED.last_updated,
                        highest_price=GREATEST(COALESCE(positions.highest_price, 0), EXCLUDED.highest_price)
                ''', (ticker, qty, avg_cost, now, now, max(avg_cost, existing_highest)))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error updating position for {ticker}: {e}", exc_info=True)
                self.conn.rollback()

    def update_highest_price(self, ticker: str, current_price: float) -> None:
        """Updates highest_price if the current price is higher."""
        with self._lock:
            try:
                self.cursor.execute('''
                    UPDATE positions
                    SET highest_price = GREATEST(COALESCE(highest_price, 0), %s)
                    WHERE ticker = %s
                ''', (current_price, ticker))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error updating highest price for {ticker}: {e}", exc_info=True)
                self.conn.rollback()

    def update_sl_tp(self, ticker: str, sl_price: float | None, tp_price: float | None) -> None:
        with self._lock:
            try:
                self.cursor.execute('''
                    UPDATE positions SET sl_price = %s, tp_price = %s
                    WHERE ticker = %s
                ''', (sl_price, tp_price, ticker))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error updating SL/TP for {ticker}: {e}", exc_info=True)
                self.conn.rollback()

    def remove_position(self, ticker: str) -> None:
        with self._lock:
            try:
                self.cursor.execute("DELETE FROM positions WHERE ticker = %s", (ticker,))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error removing position for {ticker}: {e}", exc_info=True)
                self.conn.rollback()

    def get_all_positions(self) -> pd.DataFrame:
        with self._lock:
            try:
                return pd.read_sql_query("SELECT * FROM positions", self.engine)
            except Exception as e:
                logger.error(f"Error fetching all positions: {e}", exc_info=True)
                return pd.DataFrame()

    # --- PENDING ORDERS ---

    def add_pending_order(self, order_id: int, ticker: str, action: str, qty: float, status: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            try:
                self.cursor.execute('''
                    INSERT INTO pending_orders (order_id, ticker, action, quantity, status, submitted_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id) DO UPDATE SET
                        ticker=EXCLUDED.ticker,
                        action=EXCLUDED.action,
                        quantity=EXCLUDED.quantity,
                        status=EXCLUDED.status,
                        submitted_at=EXCLUDED.submitted_at
                ''', (order_id, ticker, action, qty, status, now))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error adding pending order {order_id}: {e}", exc_info=True)
                self.conn.rollback()

    def remove_pending_order(self, order_id: int) -> None:
        with self._lock:
            try:
                self.cursor.execute("DELETE FROM pending_orders WHERE order_id = %s", (order_id,))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error removing pending order {order_id}: {e}", exc_info=True)
                self.conn.rollback()

    def get_pending_orders(self) -> pd.DataFrame:
        with self._lock:
            try:
                return pd.read_sql_query("SELECT * FROM pending_orders", self.engine)
            except Exception as e:
                logger.error(f"Error fetching pending orders: {e}", exc_info=True)
                return pd.DataFrame()

    # --- BALANCE ---

    def update_balance(self, cash: float) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            try:
                self.cursor.execute('''
                    INSERT INTO balance (id, cash_balance, last_updated)
                    VALUES (1, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        cash_balance=EXCLUDED.cash_balance,
                        last_updated=EXCLUDED.last_updated
                ''', (cash, now))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Error updating balance: {e}", exc_info=True)
                self.conn.rollback()

    def get_balance(self) -> float:
        with self._lock:
            try:
                self.cursor.execute("SELECT cash_balance FROM balance WHERE id = 1")
                result = self.cursor.fetchone()
                return result[0] if result else 0.0
            except Exception as e:
                logger.error(f"Error fetching balance: {e}", exc_info=True)
                return 0.0