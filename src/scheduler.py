import time
import os
import schedule
import threading
import telebot
import asyncio
import socket
import numpy as np
import pandas as pd
import sentry_sdk
import logging
from sentry_sdk.integrations.logging import LoggingIntegration
from datetime import datetime
from config import config
from notifier import TelegramNotifier
from logger import logger

sentry_logging = LoggingIntegration(
    level=logging.INFO,        # Capture info and above as breadcrumbs
    event_level=logging.ERROR   # Send errors as events
)

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    environment=os.getenv("ENVIRONMENT", "production"),
    traces_sample_rate=1.0,  # Adjust in high-volume production if needed
    integrations=[sentry_logging],
)

notifier = TelegramNotifier()

try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from main import run_trading_bot
from reconciler import Reconciler
from history_logger import HistoryLogger
from state_memory import StateMemory
from connector import IBConnector
from session_window import should_be_connected, set_paused, is_paused, is_attended_hours


# GLOBAL PERSISTENT CONNECTION

persistent_broker = IBConnector(client_id=1)


# LIVE PRICE CACHE

live_prices_cache = {}
live_prices_cache_time = 0
live_prices_lock = threading.Lock()

# FORCE RUN QUEUE

_force_run_requested = False
_force_run_lock = threading.Lock()

_gateway_requested = False
_gateway_lock = threading.Lock()

# TELEGRAM BOT
#
# The VPS <-> api.telegram.org long-polling connection periodically hits
# transient network blips (connection reset / read timeout / 502), which is
# normal for any long-polling bot on the public internet. By default telebot
# does NOT retry a failed request — a single blip tears down and restarts the
# whole polling thread (visible as "Infinity polling exception" in Sentry).
# Enabling RETRY_ON_ERROR retries the request itself first, so brief blips no
# longer cause a full reconnect/log-storm.
telebot.apihelper.RETRY_ON_ERROR = True
telebot.apihelper.MAX_RETRIES = 3
telebot.apihelper.RETRY_TIMEOUT = 2

bot = telebot.TeleBot(config.telegram_token)


def is_authorized(message) -> bool:
    return str(message.chat.id) == str(config.telegram_chatid)



# /help — List of commands

@bot.message_handler(commands=['help', 'start'])
def handle_help(message):
    if not is_authorized(message):
        return

    msg = (
        "<b>Insider Bot — Commands</b>\n\n"
        "<b>/status</b> — Portfolio status (positions, P&L, SL/TP)\n"
        "<b>/pending</b> — Pending orders\n"
        "<b>/today</b> — Today's trades and signals\n"
        "<b>/paper</b> — Paper experiment status (5 strategies)\n"
        "<b>/connection</b> — IBKR connection status\n"
        "<b>/force</b> — Immediate bot run\n"
        "<b>/gateway</b> — Start IB Gateway\n"
        "<b>/pause</b> — Disconnect from IBKR and stay disconnected (survives restarts)\n"
        "<b>/resume</b> — Reconnect and resume\n"
        "<b>/help</b> — This message"
    )
    bot.send_message(message.chat.id, msg, parse_mode='HTML')


# /status Positions + P&L + SL/TP + Summary

@bot.message_handler(commands=['pause'])
def handle_stop(message):
    if not is_authorized(message): return
    set_paused(True)

    if persistent_broker.is_connected():
        persistent_broker.disconnect()

    bot.send_message(message.chat.id, "<b>PAUSED</b>\nBot disconnected from IBKR and will stay disconnected (also after restarts) until /resume.", parse_mode='HTML')

@bot.message_handler(commands=['resume'])
def handle_resume(message):
    if not is_authorized(message): return
    set_paused(False)
    if should_be_connected() and is_attended_hours():
        bot.send_message(message.chat.id, "<b>RESUMED</b>\nConnecting to IBKR — approve 2FA on your phone if asked.", parse_mode='HTML')
        threading.Thread(target=_connect_now, daemon=True).start()
    else:
        bot.send_message(message.chat.id, "<b>RESUMED</b>\nNobody to approve 2FA now — the bot will connect at 14:30 on the next trading day.", parse_mode='HTML')

@bot.message_handler(commands=['status'])
def handle_status(message):
    if not is_authorized(message):
        return

    bot.send_message(message.chat.id, "Querying data...")

    try:
        memory = StateMemory()
        positions_df = memory.get_all_positions()
        cash = memory.get_balance()

        if positions_df.empty:
            bot.send_message(
                message.chat.id,
                f"<b>Portfolio Status</b>\n\n"
                f"Cash: <b>${cash:,.2f}</b>\n"
                f"Positions: <b>0 pcs</b>\n",
                parse_mode='HTML'
            )
            return

        with live_prices_lock:
            live_prices = dict(live_prices_cache)
            cache_ts = live_prices_cache_time

        cache_age_sec = (time.time() - cache_ts) if cache_ts else None

        lines = []
        total_invested = 0.0
        total_current = 0.0
        total_pnl = 0.0

        for _, row in positions_df.iterrows():
            ticker = row['ticker']
            qty = float(row['quantity'])
            avg_cost = row['average_cost']
            entry_date = row['entry_date']

            invested = qty * avg_cost
            total_invested += invested

            live_price = live_prices.get(ticker)
            if live_price:
                current_val = qty * live_price
                total_current += current_val
                pnl = current_val - invested
                pnl_pct = (pnl / invested) * 100 if invested > 0 else 0
                total_pnl += pnl
                pnl_indicator = "[PROFIT]" if pnl >= 0 else "[LOSS]"
                price_line = f"   Live price: <b>${live_price:.2f}</b> | P&L: <b>{pnl_pct:+.1f}%</b> (${pnl:+,.2f}) {pnl_indicator}"
            else:
                total_current += invested
                price_line = "   Live price: <i>not available</i>"

            sl_stored = row.get('sl_price')
            tp_stored = row.get('tp_price')
            sl_price = sl_stored if pd.notna(sl_stored) else round(avg_cost * (1 - config.stoploss_pct), 2)
            tp_price = tp_stored if pd.notna(tp_stored) else round(avg_cost * (1 + config.takeprofit_pct), 2)

            try:
                entry_np = np.datetime64(entry_date, 'D')
                today_np = np.datetime64(datetime.now().date(), 'D')
                days_held = int(np.busday_count(entry_np, today_np))
            except Exception:
                days_held = "?"

            max_hold = getattr(config, 'max_hold_days', 180)
            phase_line = f"   SL: ${sl_price} | TP: ${tp_price}"

            lines.append(
                f"<b>{ticker}</b> — {qty:g} pcs @ ${avg_cost:.2f}\n"
                f"{price_line}\n"
                f"{phase_line}\n"
                f"   Entry: {entry_date} ({days_held}/{max_hold} days)"
            )

        positions_text = "\n\n".join(lines)

        total_equity = cash + total_current
        start_capital = getattr(config, 'start_capital', 10000.0)
        total_return_pct = ((total_equity - start_capital) / start_capital) * 100 if start_capital > 0 else 0
        return_indicator = "UP" if total_return_pct >= 0 else "DOWN"

        if cache_age_sec is None:
            cache_info = "\n\n<i>Live prices not cached yet.</i>"
        elif cache_age_sec < 180:
            cache_info = f"\n\n<i>Prices age: {int(cache_age_sec)} sec</i>"
        else:
            cache_info = f"\n\n<i>Prices have not updated for {int(cache_age_sec/60)} minutes!</i>"

        summary = (
            f"<b>Portfolio Status</b>\n\n"
            f"{'─' * 28}\n"
            f"{positions_text}\n"
            f"{'─' * 28}\n\n"
            f"Cash: <b>${cash:,.2f}</b>\n"
            f"Invested: <b>${total_current:,.2f}</b>\n"
            f"Total Equity: <b>${total_equity:,.2f}</b>\n"
            f"Total P&L: <b>${total_pnl:+,.2f}</b>\n"
            f"Return (vs ${start_capital:,.0f}): <b>{total_return_pct:+.2f}%</b> [{return_indicator}]"
            f"{cache_info}"
        )

        bot.send_message(message.chat.id, summary, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error in /status handler: {e}", exc_info=True)
        bot.send_message(message.chat.id, f"Error: {e}")



# /pending — Pending orders

@bot.message_handler(commands=['pending'])
def handle_pending(message):
    if not is_authorized(message):
        return

    try:
        memory = StateMemory()
        pending = memory.get_pending_orders()

        if pending.empty:
            bot.send_message(message.chat.id, "No pending orders.")
            return

        lines = []
        for _, row in pending.iterrows():
            action_label = "[BUY]" if row['action'] == 'BUY' else "[SELL]"
            qty = float(row['quantity'])
            lines.append(
                f"{action_label} {qty:g} pcs <b>{row['ticker']}</b>\n"
                f"   Status: {row['status']} | Submitted: {row['submitted_at']}"
            )

        msg = f"<b>Pending Orders ({len(pending)})</b>\n\n" + "\n\n".join(lines)
        bot.send_message(message.chat.id, msg, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error in /pending handler: {e}", exc_info=True)
        bot.send_message(message.chat.id, f"Error: {e}")


# /today — Today's trades and signals

@bot.message_handler(commands=['today'])
def handle_today(message):
    if not is_authorized(message):
        return

    try:
        history = HistoryLogger()
        trades_df = history.get_all_trades()
        today_str = datetime.now().strftime("%Y-%m-%d")

        if trades_df.empty:
            bot.send_message(message.chat.id, "No trades today and no historical data available.")
            return

        trades_df['date_only'] = trades_df['trade_date'].str[:10]
        today_trades = trades_df[trades_df['date_only'] == today_str]

        if today_trades.empty:
            last_date = trades_df['date_only'].max()
            last_trades = trades_df[trades_df['date_only'] == last_date]
            lines = []
            for _, row in last_trades.iterrows():
                action_label = "[BUY]" if row['action'] == 'BUY' else "[SELL]"
                qty = float(row['quantity'])
                lines.append(f"{action_label} {qty:g} pcs <b>{row['ticker']}</b> @ ${row['fill_price']:.2f}")
            msg = f"No trades today.\n\nLast trading day: <b>{last_date}</b>\n\n" + "\n".join(lines)
        else:
            lines = []
            for _, row in today_trades.iterrows():
                action_label = "[BUY]" if row['action'] == 'BUY' else "[SELL]"
                qty = float(row['quantity'])
                lines.append(f"{action_label} {qty:g} pcs <b>{row['ticker']}</b> @ ${row['fill_price']:.2f}")
            msg = f"<b>Today's Trades ({len(today_trades)})</b>\n\n" + "\n".join(lines)

        bot.send_message(message.chat.id, msg, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error in /today handler: {e}", exc_info=True)
        bot.send_message(message.chat.id, f"Error: {e}")



# /paper — Paper experiment status

@bot.message_handler(commands=['paper'])
def handle_paper(message):
    if not is_authorized(message):
        return

    bot.send_message(message.chat.id, "Querying paper experiment data...")
    try:
        from paper_logger import PaperLogger, STRATEGIES

        paper_log = PaperLogger()

        signals_df = paper_log.get_signals_df()
        if signals_df.empty:
            bot.send_message(message.chat.id, "<b>Paper Experiment</b>\n\nNo data yet.", parse_mode='HTML')
            return

        total_signals = len(signals_df)
        today_str = datetime.now().strftime('%Y-%m-%d')
        today_signals = int((signals_df['signal_date'] == today_str).sum())
        last_row = signals_df.iloc[0]
        last_str = f"{last_row['ticker']} ({last_row['signal_date']})"

        summary = paper_log.strategy_summary()
        STRAT_LABELS = {
            'strat_A_quality': ('Strategy A', 'Quality strict (F≥7)'),
            'strat_B_live_mirror': ('Strategy B', 'Live mirror'),
            'strat_C_baseline': ('Strategy C', 'Baseline (control)'),
            'strat_D_value_quality': ('Strategy D', 'Value+Quality (F≥5, EV<15)'),
            'strat_E_hyper7': ('Strategy E', 'HYPER #7'),
            'strat_F_penny': ('Strategy F', 'Penny insider ($2-$5, 5 days)'),
        }

        lines = [
            f"<b>Paper Experiment — Status</b>\n",
            f"Total signals processed: <b>{total_signals}</b>",
            f"Today: <b>{today_signals}</b>",
            f"Last signal: <b>{last_str}</b>\n",
            f"{'─' * 28}",
        ]

        if summary.empty:
            lines.append("No paper positions yet (all signals SKIP).")
        else:
            for strat in STRATEGIES:
                prefix, label = STRAT_LABELS.get(strat, ('Strat', strat))
                row = summary[summary['strategy'] == strat]
                if row.empty:
                    lines.append(f"\n<b>[{prefix}] {label}</b>\n   <i>No BUY decision yet</i>")
                    continue

                r = row.iloc[0]
                total, opens, closed, wins, losses = int(r['total_entries']), int(r['open']), int(r['closed']), int(r['wins']), int(r['losses'])
                wr, avg_pnl, med_pnl = r['win_rate_pct'], r['avg_pnl_pct'], r['median_pnl_pct']

                lines.append(f"\n<b>[{prefix}] {label}</b>\n   Total: <b>{total}</b> | Open: <b>{opens}</b> | Closed: <b>{closed}</b>")

                if closed > 0:
                    wr_status = 'HIGH' if wr >= 60 else ('MOD' if wr >= 45 else 'LOW')
                    lines.append(f"   Win rate: <b>{wr:.0f}%</b> ({wins}W / {losses}L) [{wr_status}]")
                    lines.append(f"   Avg P&L: <b>{avg_pnl:+.1f}%</b> | Med: <b>{med_pnl:+.1f}%</b>")
                else:
                    lines.append(f"   <i>No closed positions yet</i>")

        lines.append(f"\n{'─' * 28}")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode='HTML')

    except Exception as e:
        logger.error(f"Error in /paper handler: {e}", exc_info=True)
        bot.send_message(message.chat.id, f"Error in /paper: {type(e).__name__}: {e}")



# /connection — IBKR connection status

@bot.message_handler(commands=['connection'])
def handle_connection(message):
    if not is_authorized(message):
        return

    gw_running = is_gateway_running()
    connected = persistent_broker.is_connected()
    last_ka_ts = persistent_broker._last_keepalive
    last_ka = datetime.fromtimestamp(last_ka_ts).strftime('%H:%M:%S') if last_ka_ts else 'N/A'
    auth_alert = persistent_broker._auth_alert_sent

    msg = (
        f"<b>Connection Status</b>\n\n"
        f"Gateway running: <b>{'Yes' if gw_running else 'No'}</b>\n"
        f"API connection: <b>{'Active' if connected else 'NONE'}</b>\n"
        f"Auth alert sent: <b>{'Yes' if auth_alert else 'No'}</b>\n"
        f"Last keepalive: <b>{last_ka}</b>"
    )
    bot.send_message(message.chat.id, msg, parse_mode='HTML')



# /force — Immediate run

@bot.message_handler(commands=['force'])
def handle_force(message):
    if not is_authorized(message):
        return

    global _force_run_requested
    with _force_run_lock:
        already_queued = _force_run_requested
        _force_run_requested = True

    if already_queued:
        bot.send_message(message.chat.id, "A manual run is already scheduled, please wait...")
    else:
        bot.send_message(message.chat.id, "<b>Run scheduled</b> — starting within 5 seconds.", parse_mode='HTML')



# /gateway — Manual IB Gateway start

@bot.message_handler(commands=['gateway'])
def handle_gateway(message):
    if not is_authorized(message):
        return

    if is_gateway_running():
        bot.send_message(message.chat.id, "IB Gateway is already running.")
        return

    global _gateway_requested
    with _gateway_lock:
        already_queued = _gateway_requested
        _gateway_requested = True

    if already_queued:
        bot.send_message(message.chat.id, "Gateway start already scheduled, please wait...")
    else:
        bot.send_message(message.chat.id, "<b>Gateway start scheduled</b> — may take ~30 seconds.", parse_mode='HTML')



# SCHEDULED ROUTINES

def is_gateway_running() -> bool:
    for port in [4001, 4002]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                return True
    return False

def start_ib_gateway():
    # The IB Gateway lifecycle is owned exclusively by the host systemd unit
    # (ibgateway.service + its start/stop timers). The bot must NOT spawn its
    # own gateway: a second xvfb-run/IBC instance collides with the systemd one
    # on the IBC CommandServer port (7462), leaving zombie Xvfb processes and an
    # unauthenticated gateway. Here we only check and, during attended hours,
    # alert so the operator (or the 14:05 recovery timer) can bring it back.
    if not is_weekday():
        return
    if is_gateway_running():
        logger.info("IB Gateway is already running.")
        return

    logger.warning("IB Gateway is not running — systemd should recover it.")
    if is_attended_hours():
        notifier.send_message(
            "<b>IB Gateway is down.</b>\n"
            "systemd should restart it (or it comes back at 14:05). "
            "Keep your phone ready to approve 2FA."
        )


def is_weekday() -> bool:
    return datetime.now().weekday() < 5


_last_keepalive_attempt = 0

def keepalive_check():
    global _last_keepalive_attempt
    now = time.time()

    # Free up the session outside the weekly window (weekend) or while paused
    if not should_be_connected():
        if persistent_broker.is_connected():
            logger.info("Outside weekly window or paused — disconnecting broker to free up session for mobile app.")
            persistent_broker.disconnect()
        return

    if now - _last_keepalive_attempt < 55:
        return

    # Overnight drops that the gateway heals by itself are re-joined quietly. A fresh session, or one that
    # keeps failing (needs 2FA), waits for the afternoon when the user can approve it.
    if (not persistent_broker.is_connected() and not is_attended_hours()
            and (not persistent_broker.had_session
                 or persistent_broker._consecutive_failures >= persistent_broker.ALERT_AFTER_FAILURES)):
        return

    _last_keepalive_attempt = now
    if is_gateway_running():
        persistent_broker.keepalive()


def refresh_live_prices():
    if not is_weekday():
        return
        
    global live_prices_cache, live_prices_cache_time
    try:
        memory = StateMemory()
        positions_df = memory.get_all_positions()
        
        if positions_df.empty or not persistent_broker.is_connected():
            with live_prices_lock:
                live_prices_cache = {}
                live_prices_cache_time = time.time()
            return

        tickers = positions_df['ticker'].tolist()
        prices = persistent_broker.get_live_prices(tickers)

        if prices:
            with live_prices_lock:
                live_prices_cache = prices
                live_prices_cache_time = time.time()
    except Exception as e:
        logger.error(f"Error refreshing live price cache: {e}", exc_info=True)


def _check_force_run():
    global _force_run_requested
    with _force_run_lock:
        if not _force_run_requested: return
        _force_run_requested = False
    
    if is_paused():
        bot.send_message(config.telegram_chatid, "Cannot force run: Bot is PAUSED (/resume).")
        return

    logger.info("Manual run requested — starting on main thread...")
    try:
        if not is_gateway_running():
            notifier.send_message("<b>Manual run failed:</b> IB Gateway is not running.")
            return

        persistent_broker.connect_persistent()
        run_trading_bot(broker=persistent_broker)
        notifier.send_message("Manual run completed successfully.")
    except Exception as e:
        logger.error(f"Manual run error: {e}", exc_info=True)
        notifier.send_message(f"<b>Manual run failed!</b>\n<code>{e}</code>")


def _check_gateway_start():
    global _gateway_requested
    with _gateway_lock:
        if not _gateway_requested:
            return
        _gateway_requested = False
    start_ib_gateway()


def prefetch_openinsider():
    if not is_weekday():
        return
    try:
        from feeder import OpenInsiderFeeder
        feeder = OpenInsiderFeeder()
        df = feeder.get_latest_buys()
        if not df.empty:
            logger.info(f"Pre-fetch ready: {len(df)} transactions cached.")
    except Exception as e:
        logger.error(f"Pre-fetch error: {e}", exc_info=True)


def _connect_now():
    try:
        if not is_gateway_running():
            start_ib_gateway()
        if persistent_broker.connect_persistent():
            notifier.send_message("<b>Connected to IBKR.</b>")
    except Exception as e:
        logger.error(f"Connect error: {e}", exc_info=True)


def daily_connect_warning():
    if should_be_connected() and not persistent_broker.is_connected():
        notifier.send_message("<b>IBKR connection in 5 minutes.</b>\nKeep your phone ready for 2FA.")


def daily_connect():
    if should_be_connected() and not persistent_broker.is_connected():
        logger.info("Attended hours start — connecting to IBKR.")
        _connect_now()


def morning_routine():
    if not is_weekday() or is_paused():
        return
    try:
        logger.info("STARTING MORNING ROUTINE...")
        if not is_gateway_running():
            start_ib_gateway()
            time.sleep(15)

        persistent_broker.connect_persistent()
        run_trading_bot(broker=persistent_broker)
    except Exception as e:
        logger.error(f"CRITICAL ERROR IN MORNING ROUTINE: {e}", exc_info=True)
        notifier.send_message(f"<b>CRITICAL ERROR (Morning)!</b>\n<code>{e}</code>")


def evening_routine():
    if not is_weekday():
        return
    try:
        logger.info("STARTING EVENING ROUTINE (RECONCILIATION)...")
        rec = Reconciler(broker=persistent_broker)
        rec.sync_data()
        
        logger.info("Paper logger: checking exits...")
        from paper_logger import PaperLogger, combined_price_fetcher
        paper = PaperLogger(broker=persistent_broker)
        paper.update_open_positions_exits(
            price_fetcher=lambda tickers: combined_price_fetcher(persistent_broker, tickers)
        )
    except Exception as e:
        logger.error(f"CRITICAL ERROR IN EVENING ROUTINE: {e}", exc_info=True)
        notifier.send_message(f"<b>CRITICAL ERROR (Evening Reconciler)!</b>\n<code>{e}</code>")


def start_telegram_bot():
    logger.info("Telegram listener started...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=90)
        except Exception as e:
            logger.error(f"Telegram interrupted: {e}. Retrying...", exc_info=True)
            time.sleep(5)


def setup_schedule():
    schedule.every(1).minutes.do(keepalive_check)
    schedule.every(2).minutes.do(refresh_live_prices)
    schedule.every(5).seconds.do(_check_force_run)
    schedule.every(5).seconds.do(_check_gateway_start)

    schedule.every().day.at("14:25").do(daily_connect_warning)
    schedule.every().day.at("14:30").do(daily_connect)
    schedule.every().day.at("15:20").do(start_ib_gateway)
    schedule.every().day.at("15:20").do(prefetch_openinsider)
    schedule.every().day.at("15:30").do(morning_routine)
    schedule.every().day.at("22:01").do(evening_routine)
    logger.info("Scheduler configured successfully.")


# --- STARTUP ---
if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("INSIDER BOT — PERSISTENT MODE")

    if not (should_be_connected() and is_attended_hours()):
        logger.info("Outside weekly window, paused or nobody to approve 2FA — IBKR connection skipped at startup.")
    elif is_gateway_running():
        persistent_broker.connect_persistent()

    tg_thread = threading.Thread(target=start_telegram_bot, daemon=True)
    tg_thread.start()

    setup_schedule()

    now = datetime.now()
    if is_weekday() and 15 * 60 + 30 <= now.hour * 60 + now.minute <= 22 * 60:
        if is_gateway_running() and persistent_broker.is_connected():
            logger.info("STARTUP RUN — executing immediately...")
            run_trading_bot(broker=persistent_broker)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
        persistent_broker.disconnect()