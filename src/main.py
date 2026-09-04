import asyncio
import sys

try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import time
from datetime import datetime
from config import config
from reconciler import Reconciler
from engine import StrategyEngine
from guardian import Guardian
from connector import IBConnector
from exiter import Exiter
from state_memory import StateMemory
from notifier import TelegramNotifier
from logger import logger


def _run_paper_logger_entry_pass(logic_engine, broker=None):
    """
    Paper experiment, collects every raw signal, evaluates in parallel
    uses IBKR for price reinforcment, yfinance for fallback
    Errors eaten, never interrupts the flow of live trading
    """
    try:
        from paper_logger import PaperLogger
        paper = PaperLogger(broker=broker)
        raw_signals = logic_engine.get_all_raw_signals()
        if not raw_signals:
            logger.info(" Paper logger: no raw signal in process window.")
            return

        today_str = datetime.now().strftime('%Y-%m-%d')
        for sig in raw_signals:
            try:
                paper.on_insider_signal({
                    'ticker': sig['ticker'],
                    'insider_name': sig.get('insider'),
                    'title': sig.get('title'),
                    'value': sig.get('value'),
                    'insider_price': sig.get('price'),
                    'signal_date': sig.get('filing_date', today_str),
                    'entry_price': sig.get('price'),
                })
            except Exception as inner:
                logger.info(f"Paper logger {sig.get('ticker')}: {type(inner).__name__}: {inner}")
    except Exception as e:
        logger.info(f"Paper logger entry pass critical error going on: {type(e).__name__}: {e}")


def run_trading_bot(broker=None):
    """
    Main trade logic
    If the broker gets parameter, it uses that shared persistent connector
    otherwise makes its own copy for standalon run
    """
    logger.info("Starting the bot...")

    connector = None
    logic = None

    try:
        logger.info("\n[1/6] Modules under load...")
        connector = broker if broker is not None else IBConnector()
        reconciler = Reconciler(broker=connector)
        logic = StrategyEngine()
        guardian = Guardian(broker=connector)
        exiter = Exiter()
        memory = StateMemory()
        notifier = TelegramNotifier()

        logger.info("\n[2/6] Synchronization of live data and memory...")
        reconciler.sync_data()

        logger.info("\n[3/6] Open positions getting checked...")
        exit_signals= exiter.get_exit_signals()

        if exit_signals:
            for exit_order in exit_signals:
                logger.info(f" -> Exit: {exit_order['quantity']:g} pcs {exit_order['ticker']} ({exit_order['reason']})")

                order_id = connector.place_sell_order(
                    ticker=exit_order['ticker'],
                    quantity=exit_order['quantity']
                )

                if order_id:
                    memory.add_pending_order(order_id, exit_order['ticker'], 'SELL', exit_order['quantity'], 'Submitted')

                    msg = (f"<b>SELL time limit:</b>\n"
                           f"Ticker: {exit_order['ticker']}\n"
                           f"Quantity: {exit_order['quantity']:g} pcs\n"
                           f"{exit_order['days_held']} days held")
                    notifier.send_message(msg)

                time.sleep(1)
        else:
            logger.info(" -> No time exits.")

        if not connector.is_data_ok():
            logger.warning("IBKR connection not available!")
            notifier.send_message(
                "<b>No connection with broker</b>\n"
                "Synchronisation happened, no new buys."
            )
            return

        logger.info("\n[4/6] Looking for fresh signals...")
        raw_signals = logic.get_entry_signals()

        if not raw_signals:
            logger.info("No appropriate entry today.")
            return

        logger.info("\n[5/6] Risk management and position sizing...")
        approved_orders = guardian.process_signals(raw_signals)

        if not approved_orders:
            logger.info("The guardian module didn't approve any order!")
            return

        logger.info("\n[6/6] Sending bracket orders to broker...")
        for order in approved_orders:
            action_str = order.action.value if hasattr(order.action, 'value') else order.action
            logger.info(f" -> {action_str} {order.quantity:g} pcs {order.ticker} @ ${order.price}")

            # Added fallback_quantity here to match connector signature
            order_id = connector.place_bracket_order(
                ticker=order.ticker,
                quantity=order.quantity,
                fallback_quantity=order.fallback_quantity,
                entry_price=order.price
            )

            if order_id:
                memory.add_pending_order(order_id, order.ticker, order.action, order.quantity, 'PreSubmitted')

                sl_price = round(order.price * (1 - config.stoploss_pct), 2)
                tp_price = round(order.price * (1 + config.takeprofit_pct), 2)
                memory.update_sl_tp(order.ticker, sl_price, tp_price)

                msg = (f"<b>NEW BUY:</b>\n"
                       f"Ticker: {order.ticker}\n"
                       f"Quantity: {order.quantity:g} pcs\n"
                       f"Insider: {order.insider} ({order.title})")
                notifier.send_message(msg)

            time.sleep(1)

        logger.info("\nEnd of daily routine!")

    except Exception as e:
        logger.info(f"\nCritical error during runtime: {e}",exc_info=True)
        TelegramNotifier().send_message(f"<b>Critical Error!</b>\n<code>{e}</code>")
    finally:
        if logic and connector:
            logger.info("\n[7/7] Paper experiment — logging insider signals...")
            _run_paper_logger_entry_pass(logic, broker=connector)


if __name__ == "__main__":
    run_trading_bot()