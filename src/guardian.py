# guardian.py
import math
from state_memory import StateMemory
from config import config
from dataclasses import dataclass
from logger import logger

@dataclass
class ApprovedOrder:
    action: str
    ticker: str
    quantity: float            
    fallback_quantity: int      
    price: float
    insider: str
    title: str

class Guardian:
    def __init__(self, broker=None):
        logger.info("Guardian initialization...")
        self.memory = StateMemory()
        self.pos_size_pct = config.pos_size
        self.broker = broker

    def _get_live_prices(self, tickers: list[str]) -> dict[str, float]:
        """Request live prices for position sizing."""
        prices = {}
        if self.broker:
            try:
                prices = self.broker.get_live_prices(tickers) or {}
            except Exception as e:
                logger.error(f"Live market price request failed: {e}", exc_info=True)

        missing = [t for t in tickers if t not in prices]
        if missing:
            from paper_logger import yfinance_last_close
            for tkr in missing:
                price = yfinance_last_close(tkr)
                if price is not None:
                    prices[tkr] = price
                    logger.info(f"   [FALLBACK] {tkr}: yfinance price obtained at ${price:.2f}")

        return prices

    def process_signals(self, signals: list) -> list[ApprovedOrder]:
        """Signal processing, fractional quantity calculation, and cash balance check."""
        if not signals:
            return []

        logger.info("Checking entry signals and sizing positions...")

        try:
            cash = self.memory.get_balance()
            positions_df = self.memory.get_all_positions()

            invested_val = 0.0
            if not positions_df.empty:
                invested_val = (positions_df['quantity'] * positions_df['average_cost']).sum()

            total_equity = cash + invested_val
            target_pos_size = total_equity * self.pos_size_pct

            logger.info(f"Capital overview — Cash: ${cash:,.2f} | Invested: ${invested_val:,.2f} | Total Equity: ${total_equity:,.2f}")
            logger.info(f"Target position size ({self.pos_size_pct * 100:.0f}%): ${target_pos_size:,.2f}")

            live_prices = self._get_live_prices([s.ticker for s in signals])
            if live_prices:
                logger.info(f"Live prices acquired for {len(live_prices)} ticker(s).")

            approved_orders = []

            for signal in signals:
                ticker = signal.ticker
                insider_price = signal.price
                sizing_price = live_prices.get(ticker, insider_price)

                if sizing_price != insider_price:
                    diff_pct = abs(sizing_price - insider_price) / insider_price
                    logger.info(f"Price variance for {ticker}: insider ${insider_price:.2f} vs market ${sizing_price:.2f} ({diff_pct:.1%})")

                if cash >= target_pos_size and target_pos_size > 0:
                    # Fractional primary and whole share fallback logic
                    fractional_shares = round(target_pos_size / sizing_price, 4)
                    whole_shares = math.floor(target_pos_size / sizing_price)

                    if fractional_shares <= 0.0001:
                        logger.warning(f"   [REFUSED] {ticker}: Target allocation (${target_pos_size:,.2f}) too small for fractional scaling.")
                        continue

                    actual_cost = fractional_shares * sizing_price
                    
                    if whole_shares < 1:
                         logger.info(f"   [NOTE] {ticker}: Fallback whole quantity is 0. If fractional is rejected by IBKR, order will be cancelled.")

                    logger.info(f"   [APPROVED] {ticker} -> {fractional_shares} pcs (fallback: {whole_shares}) | Cost: ~${actual_cost:,.2f}")

                    approved_orders.append(ApprovedOrder(
                        action='BUY',
                        ticker=ticker,
                        quantity=fractional_shares,
                        fallback_quantity=whole_shares,
                        price=insider_price,
                        insider=signal.insider,
                        title=signal.title
                    ))

                    cash -= actual_cost

                else:
                    logger.warning(f"   [REFUSED] {ticker}: Insufficient funds. Needed: ${target_pos_size:,.2f}, Available: ${cash:,.2f}")

            logger.info(f"Guardian completed: {len(approved_orders)} order(s) approved.")
            return approved_orders

        except Exception as e:
            logger.error(f"Critical error during signal processing in Guardian: {e}", exc_info=True)
            return []