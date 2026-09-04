import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime
from feeder import OpenInsiderFeeder
from state_memory import StateMemory
from models import TradeSignal, SignalAction
from config import config
from dataclasses import replace
from logger import logger

class StrategyEngine:
    def __init__(self):
        logger.info("StrategyEngine initialization...")
        self.feeder = OpenInsiderFeeder()
        self.memory = StateMemory()

    def is_market_bullish(self) -> bool:
        """Evaluates SPY macro trend using a 200-day Moving Average."""
        if not config.market_filter:
            return True 
            
        logger.info("SPY macro trend analysis...")
        try:
            spy = yf.Ticker('SPY').history(period='1y')
            if spy.empty:
                logger.warning("Empty SPY data received, defaulting to Bullish.")
                return True
                
            spy = spy.dropna(subset=['Close'])
            
            if len(spy) < 200:
                logger.warning("Not enough data for MA200 calculation, defaulting to Bullish.")
                return True 
                
            spy['MA200'] = spy['Close'].rolling(window=200).mean()
            
            yesterday_data = spy.iloc[-2] 
            close_price = float(yesterday_data['Close'])
            ma200_price = float(yesterday_data['MA200'])

            if np.isnan(ma200_price):
                return True

            is_bullish = close_price > ma200_price
            trend_str = 'Bullish' if is_bullish else 'Bearish'
            operator = '>' if is_bullish else '<'
            logger.info(f"   {trend_str} Market: SPY (${close_price:.2f}) {operator} MA200 (${ma200_price:.2f})")
            return is_bullish
            
        except Exception as e:
            logger.error(f"Error in market filter: {e}", exc_info=True)
            return True

    def _check_pre_drop(self, ticker: str) -> tuple[bool, float | None]:
        """
        Checks if the ticker dropped at least the threshold amount
        in the last lookback period.
        """
        lookback = config.predrop_lookback
        threshold = config.predrop_threshold
        drop_type = config.predrop_type

        try:
            hist = yf.Ticker(ticker).history(period=f'{lookback + 10}d')
            if hist.empty or len(hist) < lookback:
                return False, None

            closes = hist['Close'].values
            close_now = float(closes[-1])

            if drop_type == 'return':
                close_past = float(closes[-lookback - 1]) if len(closes) > lookback else float(closes[0])
                if close_past <= 0:
                    return False, None
                drop_pct = (close_now / close_past) - 1.0
            else:
                peak = float(np.max(closes[-lookback:]))
                if peak <= 0:
                    return False, None
                drop_pct = (close_now / peak) - 1.0

            passed = drop_pct <= threshold
            return passed, drop_pct
        except Exception as e:
            logger.error(f"Pre-drop check failed for {ticker}: {e}", exc_info=True)
            return False, None

    def get_entry_signals(self) -> list[TradeSignal]:
        logger.info("\nFetching fresh data and analyzing...")
        
        # Q1 Filter
        current_month = datetime.now().month
        if config.q1_filter and current_month in [1, 2, 3]:
            logger.info("Q1 filter active, no new trades allowed.")
            return []

        # Market Filter
        if not self.is_market_bullish():
            logger.info("Trading suspended: Market is bearish.")
            return []

        raw_data = self.feeder.get_latest_buys()
        
        if raw_data.empty:
            logger.info("No insider data available for analysis.")
            return []

        # Business day calculation
        filing_dates_np = pd.to_datetime(raw_data['filing_date']).values.astype('datetime64[D]')
        today_clean = np.datetime64(datetime.now().date(), 'D')
        raw_data['bdays_passed'] = np.busday_count(filing_dates_np, today_clean)

        # Config filters
        pattern = '|'.join(config.titles)
        delay_min = config.entry_day_delay
        delay_max = config.entry_day_delay_max
        filtered_df = raw_data[
            (raw_data['value'] >= config.min_trade_value) & 
            (raw_data['price'] >= config.min_share_price) &
            (raw_data['title'].str.contains(pattern, case=False, na=False)) &
            (raw_data['bdays_passed'] >= delay_min) &
            (raw_data['bdays_passed'] <= delay_max)
        ]
        
        # Exclude currently owned or pending tickers
        current_positions = self.memory.get_all_positions()
        owned_tickers = current_positions['ticker'].tolist() if not current_positions.empty else []
        pending_orders = self.memory.get_pending_orders()
        pending_buys = pending_orders[pending_orders['action'] == 'BUY']['ticker'].tolist() if not pending_orders.empty else []
        
        exclude_set = set(owned_tickers + pending_buys)
        
        candidate_signals = []
        for _, row in filtered_df.iterrows():
            if row['ticker'] not in exclude_set:
                candidate_signals.append(TradeSignal(
                    action=SignalAction.BUY,
                    ticker=row['ticker'],
                    insider=row['insider_name'],
                    title=row['title'],
                    price=row['price'],
                    value=row['value'],
                    filed_days_ago=int(row['bdays_passed'])
                ))
        
        # Ensure unique tickers
        unique_candidates = list({v.ticker: v for v in candidate_signals}.values())

        if not unique_candidates:
            logger.info("No viable new tickers to buy.")
            return []

        final_signals = unique_candidates

        # Pre-drop filter
        if config.use_predrop and final_signals:
            threshold = config.predrop_threshold
            lookback = config.predrop_lookback
            logger.info(f"Pre-drop filter ({threshold:+.0%} drawdown / {lookback}d): evaluating {len(final_signals)} candidates...")
            
            kept = []
            for sig in final_signals:
                passed, drop_pct = self._check_pre_drop(sig.ticker)
                if passed:
                    updated_sig = replace(sig, pre_drop_pct=drop_pct)
                    kept.append(updated_sig)
                    logger.info(f"   [PASSED] {sig.ticker}: drop={drop_pct:+.1%} (≤{threshold:+.0%})")
                else:
                    drop_str = f"{drop_pct:+.1%}" if drop_pct is not None else "N/A"
                    logger.info(f"   [DENIED] {sig.ticker}: drop={drop_str}")
                    
            logger.info(f"Pre-drop summary: {len(kept)}/{len(final_signals)} passed.")
            final_signals = kept

        logger.info(f"Final approved buy signals: {len(final_signals)}")
        return final_signals

    # =========================================================================
    # PAPER EXPERIMENT
    # =========================================================================
    def get_all_raw_signals(self) -> list[dict]:
        """
        Paper forward testing: captures all insider signals with a wider entry window.
        """
        raw_data = self.feeder.get_latest_buys()
        if raw_data.empty:
            return []

        filing_dates_np = pd.to_datetime(raw_data['filing_date']).values.astype('datetime64[D]')
        today_clean = np.datetime64(datetime.now().date(), 'D')
        raw_data['bdays_passed'] = np.busday_count(filing_dates_np, today_clean)

        delay_min = config.entry_day_delay
        delay_max = config.paper_entry_delay_max
        day_filtered = raw_data[
            (raw_data['bdays_passed'] >= delay_min) &
            (raw_data['bdays_passed'] <= delay_max)
        ]

        signals = []
        seen = set()
        for _, row in day_filtered.iterrows():
            ticker = row['ticker']
            if ticker not in seen and row['price'] > 0:
                seen.add(ticker)
                fd_raw = row.get('filing_date')
                try:
                    filing_date_iso = pd.to_datetime(fd_raw).strftime('%Y-%m-%d')
                except Exception:
                    filing_date_iso = datetime.now().strftime('%Y-%m-%d')

                signals.append({
                    'ticker': ticker,
                    'insider': row['insider_name'],
                    'title': row['title'],
                    'price': row['price'],
                    'value': row['value'],
                    'filing_date': filing_date_iso,
                })

        logger.info(f"Paper experiment: Found {len(signals)} unfiltered insider signals (window: {delay_min}-{delay_max} days).")
        return signals

if __name__ == "__main__":
    engine = StrategyEngine()
    buy_signals = engine.get_entry_signals()
    
    if buy_signals:
        logger.info("\nToday's Buy List:")
        for s in buy_signals[:5]:
            logger.info(f" -> BUY: {s.ticker} (Price: ${s.price}, Value: ${s.value:,.0f})")
    else:
        logger.info("\nNo opportunities today.")