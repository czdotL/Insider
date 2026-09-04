import asyncio
import threading
import time
from datetime import datetime
from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder
from config import config
from notifier import TelegramNotifier
from logger import logger

try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


class IBConnector:
    def __init__(self, host='127.0.0.1', port=4001, client_id=1):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        self.notifier = TelegramNotifier()
        self._last_keepalive = 0
        self._auth_alert_sent = False
        self._data_ok = True
        self._last_1100_alert = 0
        self.account = config.ibkr_account

        self._lock = threading.RLock()

        self.ib.disconnectedEvent += self._on_disconnect
        self.ib.errorEvent += self._on_error

    # Helper to prevent Telegram spam outside trading hours
    def _is_active_hours(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        if now.hour < 15 or now.hour > 22:
            return False
        if now.hour == 15 and now.minute < 15:
            return False
        if now.hour == 22 and now.minute > 15:
            return False
        return True

    # Connection management
    def _on_disconnect(self):
        logger.warning("IBKR connection lost!")
        if self._is_active_hours():
            self.notifier.send_message(
                "<b>IBKR connection lost!</b>\n"
                "The bot is trying to reconnect.\n"
                "If it fails, you might need to <b>re-authenticate on your phone</b>."
            )

    def _on_error(self, reqId, errorCode, errorString, contract):
        if errorCode == 1100:
            self._data_ok = False
            now = time.time()
            if now - self._last_1100_alert > 300:
                self._last_1100_alert = now
                logger.warning("IBKR 1100: Data connection lost!")
                if self._is_active_hours():
                    self.notifier.send_message(
                        "<b>IBKR data connection lost!</b>\n"
                        "Trading suspended until connection is restored."
                    )
        elif errorCode == 1102:
            self._data_ok = True
            logger.info("IBKR 1102: Data connection restored!")
            if self._is_active_hours():
                self.notifier.send_message("<b>IBKR data connection restored!</b>")
        elif errorCode == 1101:
            self._data_ok = True
            logger.warning("IBKR 1101: Connection restored (with data loss)!")
            if self._is_active_hours():
                self.notifier.send_message("<b>IBKR connection restored (with data loss).</b>")

    def is_data_ok(self):
        return self._data_ok and self.ib.isConnected()

    def _ensure_event_loop(self):
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def connect(self):
        with self._lock:
            if not self.ib.isConnected():
                try:
                    self._ensure_event_loop()
                    self.ib.connect(self.host, self.port, clientId=self.client_id)
                    self.ib.reqMarketDataType(3)
                    self._auth_alert_sent = False
                    acct_msg = f" (Account: {self.account})" if self.account else ""
                    logger.info(f"Connected to IBKR{acct_msg}.")
                except Exception as e:
                    logger.error(f"Connection failed: {e}", exc_info=True)
                    self._handle_auth_failure(e)

    def connect_persistent(self, max_retries=3, retry_delay=10):
        with self._lock:
            for attempt in range(1, max_retries + 1):
                if self.ib.isConnected():
                    return True
                logger.info(f"Connection attempt ({attempt}/{max_retries})...")
                try:
                    self._ensure_event_loop()
                    self.ib.connect(self.host, self.port, clientId=self.client_id)
                    self.ib.reqMarketDataType(3)
                    self._auth_alert_sent = False
                    logger.info("Persistent connection established with IBKR.")
                    return True
                except Exception as e:
                    logger.warning(f"Attempt {attempt} failed: {e}")
                    if attempt < max_retries:
                        time.sleep(retry_delay)

            self._handle_auth_failure("Failed to connect after multiple attempts.")
            return False

    def _handle_auth_failure(self, error):
        if not self._auth_alert_sent:
            self._auth_alert_sent = True
            if self._is_active_hours():
                self.notifier.send_message(
                    "<b>IBKR AUTHENTICATION REQUIRED!</b>\n\n"
                    "The bot cannot connect to IB Gateway.\n"
                    "The <b>session likely expired</b> and requires 2FA on your phone.\n\n"
                    f"Error: <code>{error}</code>\n\n"
                    "Log into IB Gateway, the bot will reconnect automatically."
                )

    def keepalive(self):
        with self._lock:
            if not self.ib.isConnected():
                logger.warning("Keepalive: No connection, reconnecting...")
                return self.connect_persistent(max_retries=2, retry_delay=5)
            try:
                self.ib.reqCurrentTime()
                self._last_keepalive = time.time()
                return True
            except Exception as e:
                logger.error(f"Keepalive error: {e}", exc_info=True)
                return False

    def health_check(self):
        with self._lock:
            status = {
                'connected': self.ib.isConnected(),
                'last_keepalive': self._last_keepalive,
                'auth_alert_sent': self._auth_alert_sent,
            }
            if not status['connected']:
                success = self.connect_persistent(max_retries=2, retry_delay=5)
                status['reconnected'] = success
                status['connected'] = self.ib.isConnected()
            return status

    def disconnect(self):
        with self._lock:
            if self.ib.isConnected():
                self.ib.disconnect()

    def is_connected(self):
        return self.ib.isConnected()

    # QUERIES

    def get_account_summary(self):
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return None
            try:
                acct = self.account or ''
                values = self.ib.accountValues(acct) if acct else self.ib.accountValues()

                cash_by_currency = {}
                fx_rates = {}

                for val in values:
                    if acct and val.account != acct:
                        continue
                    if val.tag == 'CashBalance' and val.currency != 'BASE':
                        cash_by_currency[val.currency] = float(val.value)
                    elif val.tag == 'ExchangeRate':
                        fx_rates[val.currency] = float(val.value)

                usd_fx = fx_rates.get('USD', 1.0)

                total_usd = 0.0
                for currency, amount in cash_by_currency.items():
                    if currency == 'USD':
                        total_usd += amount
                        logger.info(f"   USD cash: ${amount:,.2f}")
                    else:
                        rate = fx_rates.get(currency, 0)
                        if rate > 0:
                            converted = amount * rate / usd_fx
                            total_usd += converted
                            logger.info(f"   {currency} cash: {amount:,.0f} × {rate:.6f} / {usd_fx:.2f} = ${converted:,.2f}")
                        else:
                            logger.warning(f"   {currency} cash: {amount:,.0f} — no fx rate, skipping")

                logger.info(f"   Total cash: ${total_usd:,.2f}")
                return total_usd
            except Exception as e:
                logger.error(f"Error fetching account summary: {e}", exc_info=True)
                return None

    def get_positions(self):
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return []
            positions_data = []
            try:
                positions = self.ib.positions()
                for pos in positions:
                    if pos.position != 0:
                        if self.account and pos.account != self.account:
                            continue
                        positions_data.append({
                            'symbol': pos.contract.symbol,
                            'qty': pos.position,
                            'avg_cost': pos.avgCost
                        })
                return positions_data
            except Exception as e:
                logger.error(f"Error fetching positions: {e}", exc_info=True)
                return []

    def get_live_prices(self, tickers):
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return {}
            prices = {}
            try:
                contracts = []
                for ticker in tickers:
                    c = Stock(ticker, 'SMART', 'USD')
                    contracts.append(c)

                self.ib.qualifyContracts(*contracts)
                ticker_data_list = self.ib.reqTickers(*contracts)
                self.ib.sleep(1)

                for ticker, data in zip(tickers, ticker_data_list):
                    price = data.marketPrice()
                    if not price or price <= 0:
                        price = data.last if data.last and data.last > 0 else data.close
                    if price and price > 0:
                        prices[ticker] = float(price)
            except Exception as e:
                logger.error(f"Error fetching live prices: {e}", exc_info=True)
            return prices

    def get_recent_fills(self):
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return []
            logger.info("IBKR: Fetching recent fills...")
            fills_data = []
            try:
                self.ib.reqExecutions()
                self.ib.sleep(1)
                for fill in self.ib.fills():
                    if self.account and fill.execution.acctNumber != self.account:
                        continue
                    action_mapped = "BUY" if fill.execution.side == "BOT" else "SELL"
                    fills_data.append({
                        'exec_id': fill.execution.execId,
                        'order_id': fill.execution.orderId,
                        'time': fill.execution.time.strftime("%Y-%m-%d %H:%M:%S") if fill.execution.time else "",
                        'ticker': fill.contract.symbol,
                        'action': action_mapped,
                        'quantity': float(fill.execution.shares),
                        'fill_price': float(fill.execution.price),
                        'commission': float(fill.commissionReport.commission) if fill.commissionReport else 0.0
                    })
                return fills_data
            except Exception as e:
                logger.error(f"Error fetching executions: {e}", exc_info=True)
                return []

    def get_open_orders(self):
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return []
            open_order_ids = []
            try:
                self.ib.reqAllOpenOrders()
                self.ib.sleep(1)
                for trade in self.ib.openTrades():
                    if self.account and trade.order.account and trade.order.account != self.account:
                        continue
                    open_order_ids.append(trade.order.orderId)
                return open_order_ids
            except Exception as e:
                logger.error(f"Error fetching open orders: {e}", exc_info=True)
                return []

    def get_open_orders_detailed(self):
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return []
            orders = []
            try:
                self.ib.reqAllOpenOrders()
                self.ib.sleep(1)
                for trade in self.ib.openTrades():
                    if self.account and trade.order.account and trade.order.account != self.account:
                        continue
                    o = trade.order
                    price = o.lmtPrice if o.orderType == 'LMT' else o.auxPrice
                    orders.append({
                        'order_id': o.orderId,
                        'ticker': trade.contract.symbol,
                        'action': o.action,
                        'order_type': o.orderType,
                        'price': float(price) if price else 0.0,
                        'quantity': float(o.totalQuantity),
                        'parent_id': o.parentId,
                    })
                return orders
            except Exception as e:
                logger.error(f"Error fetching detailed open orders: {e}", exc_info=True)
                return []

    # Execution
    def place_bracket_order(self, ticker, quantity, fallback_quantity, entry_price):
        """Places a bracket order with a fractional quantity. Re-transmits whole shares on rejection."""
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return None

            contract = Stock(ticker, 'SMART', 'USD')
            self.ib.qualifyContracts(contract)

            [ticker_data] = self.ib.reqTickers(contract)
            market_price = ticker_data.marketPrice()
            if not market_price or market_price <= 0:
                market_price = ticker_data.last if ticker_data.last > 0 else ticker_data.close

            actual_price = market_price if (market_price and market_price > 0) else entry_price

            qty = float(quantity)
            sl_price = round(actual_price * (1 - config.stoploss_pct), 2)
            tp_price = round(actual_price * (1 + config.takeprofit_pct), 2)

            logger.info(f"Calculation base: ${actual_price:.2f} | SL: ${sl_price} | TP: ${tp_price}")

            parent = MarketOrder('BUY', qty)
            parent.orderId = self.ib.client.getReqId()
            parent.tif = 'DAY'
            parent.transmit = False
            if self.account:
                parent.account = self.account

            stop_loss = StopOrder('SELL', qty, sl_price)
            stop_loss.orderId = self.ib.client.getReqId()
            stop_loss.parentId = parent.orderId
            stop_loss.tif = 'GTC'
            stop_loss.transmit = False
            if self.account:
                stop_loss.account = self.account

            take_profit = LimitOrder('SELL', qty, tp_price)
            take_profit.orderId = self.ib.client.getReqId()
            take_profit.parentId = parent.orderId
            take_profit.tif = 'GTC'
            take_profit.transmit = True
            if self.account:
                take_profit.account = self.account

            fractional_error_caught = False

            def fractional_error_handler(reqId, errorCode, errorString, _contract):
                nonlocal fractional_error_caught
                if reqId in (parent.orderId, stop_loss.orderId, take_profit.orderId):
                    if errorCode in (201, 10149, 10248, 10249) or "fractional" in errorString.lower():
                        fractional_error_caught = True

            self.ib.errorEvent += fractional_error_handler

            try:
                self.ib.placeOrder(contract, parent)
                self.ib.placeOrder(contract, stop_loss)
                self.ib.placeOrder(contract, take_profit)

                self.ib.sleep(1.5)

                self.ib.errorEvent -= fractional_error_handler

                if fractional_error_caught:
                    if fallback_quantity > 0:
                        logger.warning(f"Fractional shares rejected for {ticker}. Falling back to {fallback_quantity} whole shares.")

                        parent.totalQuantity = float(fallback_quantity)
                        stop_loss.totalQuantity = float(fallback_quantity)
                        take_profit.totalQuantity = float(fallback_quantity)

                        self.ib.placeOrder(contract, parent)
                        self.ib.placeOrder(contract, stop_loss)
                        self.ib.placeOrder(contract, take_profit)
                        self.ib.sleep(1)
                    else:
                        logger.warning(f"Fractional shares rejected for {ticker} and fallback whole quantity is 0. Aborting order.")
                        return None

                acct_msg = f" [Account: {self.account}]" if self.account else ""
                logger.info(f"Bracket order finalized: {ticker} (orderId: {parent.orderId}){acct_msg}")
                return parent.orderId

            except Exception as e:
                self.ib.errorEvent -= fractional_error_handler
                logger.error(f"Order submission error: {e}", exc_info=True)
                self.notifier.send_message(
                    f"<b>BROKER ERROR (Buy)</b>\n"
                    f"Failed to submit {ticker} order.\nReason: <code>{e}</code>"
                )
                return None

    def place_sell_order(self, ticker, quantity):
        with self._lock:
            self._ensure_event_loop()
            self.connect()
            if not self.ib.isConnected():
                return None
            contract = Stock(ticker, 'SMART', 'USD')
            self.ib.qualifyContracts(contract)
            sell_order = MarketOrder('SELL', float(quantity))
            if self.account:
                sell_order.account = self.account
            try:
                trade = self.ib.placeOrder(contract, sell_order)
                acct_msg = f" [Account: {self.account}]" if self.account else ""
                logger.info(f"Sell order submitted: {ticker} (orderId: {trade.order.orderId}){acct_msg}")
                self.ib.sleep(1)
                return trade.order.orderId
            except Exception as e:
                logger.error(f"Sell order error: {e}", exc_info=True)
                self.notifier.send_message(
                    f"<b>BROKER ERROR (Sell)</b>\n"
                    f"Failed to submit {ticker} sell order.\nReason: <code>{e}</code>"
                )
                return None