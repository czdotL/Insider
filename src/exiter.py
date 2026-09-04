"""
Time-based exit manager (Exiter)
"""
import pandas as pd
import numpy as np
from datetime import datetime
from state_memory import StateMemory
from config import config
from dataclasses import dataclass
from logger import logger

@dataclass
class ExitOrder:
    action: str
    ticker: str
    quantity: int
    days_held: int
    reason: str

class Exiter:
    def __init__(self):
        logger.info("Exiter initialization...")
        self.memory = StateMemory()
        self.max_hold_days = config.max_hold_days

    def get_exit_signals(self) -> list[ExitOrder]:
        try:
            positions_df = self.memory.get_all_positions()

            if positions_df.empty:
                return []

            logger.info(f"Checking {len(positions_df)} open positions (max hold: {self.max_hold_days} days)...")

            entry_dates_np = pd.to_datetime(positions_df['entry_date']).values.astype('datetime64[D]')
            today_clean = np.datetime64(datetime.now().date(), 'D')
            positions_df['days_held'] = np.busday_count(entry_dates_np, today_clean)

            pending_orders = self.memory.get_pending_orders()
            pending_sells = pending_orders[pending_orders['action'] == 'SELL']['ticker'].tolist() if not pending_orders.empty else []

            exit_signals = []

            for _, row in positions_df.iterrows():
                ticker = row['ticker']
                days_held = int(row['days_held'])
                quantity = int(row['quantity'])

                if ticker in pending_sells:
                    logger.info(f"   [SKIPPED] {ticker}: Sell already in progress.")
                    continue

                # Max hold check
                if days_held >= self.max_hold_days:
                    exit_signals.append(ExitOrder(
                        action='SELL',
                        ticker=ticker,
                        quantity=quantity,
                        days_held=days_held,
                        reason='MAX_HOLD_TIME_REACHED'
                    ))
                    logger.info(f"   [EXIT] {ticker}: {days_held} days held (limit reached: {self.max_hold_days})")
                else:
                    logger.info(f"   [HOLD] {ticker}: {days_held}/{self.max_hold_days} days — broker SL/TP is active")

            return exit_signals

        except Exception as e:
            logger.error(f"Error calculating exit signals: {e}", exc_info=True)
            return []