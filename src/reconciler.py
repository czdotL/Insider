from connector import IBConnector
from state_memory import StateMemory
from history_logger import HistoryLogger
from notifier import TelegramNotifier
from logger import logger

class Reconciler:
    def __init__(self, broker=None):
        logger.info("Reconciler initialization...")
        self.broker = broker if broker is not None else IBConnector()
        self._owns_broker = broker is None  
        self.memory = StateMemory()
        self.history = HistoryLogger()
        self.notifier = TelegramNotifier()

    def sync_data(self) -> None:
        logger.info("Starting synchronization with IBKR...")

        self.broker.connect()

        if not self.broker.is_connected():
            logger.error("Failed to connect to the broker. Synchronization aborted!")
            return

        try:
            # --- A) BALANCE ---
            logger.info("Fetching live account balance...")
            live_balance = self.broker.get_account_summary()
            if live_balance is not None:
                old_balance = self.memory.get_balance()
                self.memory.update_balance(live_balance)
                if abs(live_balance - old_balance) > 1:
                    logger.info(f"Balance updated: ${old_balance:,.2f} → ${live_balance:,.2f}")
            else:
                logger.warning("Account balance fetch failed — retaining previous value!")

            # --- B) POSITIONS ---
            logger.info("Fetching live positions...")
            live_positions = self.broker.get_positions()
            db_positions_df = self.memory.get_all_positions()
            db_tickers = db_positions_df['ticker'].tolist() if not db_positions_df.empty else []
            live_tickers = [p['symbol'] for p in live_positions] if live_positions else []

            for ticker in db_tickers:
                if ticker not in live_tickers:
                    pos_row = db_positions_df[db_positions_df['ticker'] == ticker].iloc[0]
                    # Fractional upgrade: parse as float
                    qty = float(pos_row['quantity'])
                    avg_cost = pos_row['average_cost']
                    entry_date = pos_row['entry_date']
                    
                    self.memory.remove_position(ticker)
                    logger.info(f"[REMOVED] {ticker} removed from memory (no longer held at broker).")
                    
                    # Formatting with :g ensures 1.5 shows as 1.5, and 2.0 shows as 2
                    msg = (
                        f"<b>Position disappeared from broker!</b>\n\n"
                        f"<b>{ticker}</b> — {qty:g} pcs @ ${avg_cost:.2f}\n"
                        f"Entry: {entry_date}\n\n"
                        f"Likely SL/TP bracket order executed.\n"
                        f"Check execution price in IBKR!"
                    )
                    self.notifier.send_message(msg)

            if live_positions:
                for p in live_positions:
                    self.memory.update_position(p['symbol'], p['qty'], p['avg_cost'])

            # --- B2) SL/TP SYNCHRONIZATION ---
            try:
                detailed_orders = self.broker.get_open_orders_detailed()
                for ticker in live_tickers:
                    sl = None
                    tp = None
                    for o in detailed_orders:
                        if o['ticker'] == ticker and o['action'] == 'SELL':
                            if o['order_type'] == 'STP':
                                sl = o['price']
                            elif o['order_type'] == 'LMT':
                                tp = o['price']
                    if sl or tp:
                        self.memory.update_sl_tp(ticker, sl, tp)
            except Exception as e:
                logger.error(f"SL/TP synchronization error: {e}", exc_info=True)

            # --- C) PENDING ORDERS AND FILLS ---
            logger.info("Checking open orders and recent fills...")
            live_open_order_ids = self.broker.get_open_orders()
            recent_fills = self.broker.get_recent_fills()
            db_pending_orders = self.memory.get_pending_orders()

            fills_by_order_id = {}
            for fill in recent_fills:
                oid = fill['order_id']
                if oid not in fills_by_order_id:
                    fills_by_order_id[oid] = []
                fills_by_order_id[oid].append(fill)

            if not db_pending_orders.empty:
                for index, row in db_pending_orders.iterrows():
                    db_order_id = row['order_id']
                    original_qty = float(row['quantity'])

                    if db_order_id in fills_by_order_id:
                        total_filled_now = 0.0

                        for fill_data in fills_by_order_id[db_order_id]:
                            total_filled_now += float(fill_data['quantity'])
                            logger.info(f"[FILL] {fill_data['action']} {fill_data['quantity']:g} pcs {fill_data['ticker']} @ ${fill_data['fill_price']:.2f}")

                            self.history.log_trade(
                                trade_date=fill_data['time'],
                                ticker=fill_data['ticker'],
                                action=fill_data['action'],
                                quantity=fill_data['quantity'],
                                fill_price=fill_data['fill_price'],
                                commission=fill_data['commission'],
                                order_id=db_order_id
                            )

                        if db_order_id in live_open_order_ids:
                            remaining_qty = original_qty - total_filled_now
                            msg = (f"<b>Partial fill ({row['ticker']})!</b>\n"
                                   f"Requested: {original_qty:g} pcs | Filled: {total_filled_now:g} pcs | Remaining: {remaining_qty:g} pcs.")
                            logger.warning(msg)
                            self.notifier.send_message(msg)
                            self.memory.add_pending_order(db_order_id, row['ticker'], row['action'], remaining_qty, "PartiallyFilled")
                        else:
                            logger.info(f"[COMPLETED] Order fully filled: {row['ticker']}")
                            self.memory.remove_pending_order(db_order_id)

                    elif db_order_id not in live_open_order_ids:
                        msg = (f"<b>Order cancelled / rejected!</b>\n"
                               f"Ticker: {row['ticker']} | {row['action']} {original_qty:g} pcs")
                        logger.warning(msg)
                        self.notifier.send_message(msg)
                        self.memory.remove_pending_order(db_order_id)
            else:
                logger.info("No pending orders found in database.")

            # --- D) DAILY EQUITY CURVE ---
            logger.info("Saving daily equity curve state...")
            cash = self.memory.get_balance()

            invested_val = 0.0
            if not db_positions_df.empty:
                invested_val = (db_positions_df['quantity'] * db_positions_df['average_cost']).sum()

            total_equity = cash + invested_val
            self.history.log_daily_equity(cash, invested_val, total_equity)
            logger.info(f"Equity overview — Cash: ${cash:,.2f} | Equities: ${invested_val:,.2f} | Total: ${total_equity:,.2f}")

        except Exception as e:
            logger.error(f"Unexpected error during data synchronization: {e}", exc_info=True)

        finally:
            if self._owns_broker:
                self.broker.disconnect()

        logger.info("Synchronization completed.")