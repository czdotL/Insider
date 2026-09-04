import pandas as pd
import requests
import json
import os
from datetime import datetime
from notifier import TelegramNotifier
from bs4 import BeautifulSoup
from logger import logger

class OpenInsiderFeeder:
    def __init__(self):
        self.cache_file = "feeder_cache.json"
        logger.info("Feeder initialization (OpenInsider Scraper)...")
        self.base_url = "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=0&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&xp=1&vl=&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&pt=1&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&sortdir=desc&cnt=100&page=1"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Connection': 'keep-alive'
        }
        self.notifier = TelegramNotifier()

    def get_latest_buys(self):
        today = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame()

        # Cache check
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    cache_data = json.load(f)
                    if cache_data.get('date') == today:
                        logger.info("Loading data from local cache...")
                        df = pd.DataFrame(cache_data['data'])
            except Exception as e:
                logger.error(f"Error reading cache: {e}", exc_info=True)

        # Download if no cache
        if df.empty:
            logger.info("Downloading raw data from OpenInsider...")
            try:
                response = requests.get(self.base_url, headers=self.headers, timeout=15)
                if response.status_code != 200:
                    self.notifier.send_message(f"<b>Feeder Error!</b> (Status: {response.status_code})")
                    return pd.DataFrame()

                soup = BeautifulSoup(response.text, 'html.parser')
                table = soup.find('table', {'class': 'tinytable'})

                if not table:
                    self.notifier.send_message(
                        "<b>Feeder: Table not found on OpenInsider page!</b>\n"
                        "The page structure might have changed."
                    )
                    return pd.DataFrame()

                rows = []
                for tr in table.find_all('tr')[1:]:
                    tds = tr.find_all('td')
                    if len(tds) < 12:
                        continue
                    rows.append({
                        'filing_date': tds[1].get_text(strip=True),
                        'trade_date':  tds[2].get_text(strip=True),
                        'ticker':      tds[3].get_text(strip=True),
                        'insider_name': tds[5].get_text(strip=True),
                        'title':       tds[6].get_text(strip=True),
                        'price':       tds[8].get_text(strip=True),
                        'qty':         tds[9].get_text(strip=True),
                        'owned':       tds[10].get_text(strip=True),
                        'value':       tds[12].get_text(strip=True)
                    })

                df = pd.DataFrame(rows)

                # Alert if fresh scraping yielded 0 rows
                if df.empty:
                    self.notifier.send_message(
                        "<b>Feeder: 0 transactions from OpenInsider!</b>\n"
                        "The table exists but is empty. Might be a weekend, holiday, or structural change."
                    )
                    return pd.DataFrame()

                # Save to cache
                with open(self.cache_file, 'w') as f:
                    json.dump({'date': today, 'data': df.to_dict(orient='records')}, f)

            except requests.exceptions.Timeout:
                self.notifier.send_message("<b>Feeder Timeout!</b> OpenInsider did not respond within 15 seconds.")
                return pd.DataFrame()
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                self.notifier.send_message(f"<b>Scraper Crash!</b>\nReason: <code>{e}</code>")
                return pd.DataFrame()

        # Data cleaning
        if not df.empty:
            for col in ['price', 'qty', 'owned', 'value']:
                df[col] = df[col].replace(r'[\$,+]', '', regex=True).replace('', '0')
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            logger.info(f"Data ready: {len(df)} transactions.")

        return df


if __name__ == "__main__":
    feeder = OpenInsiderFeeder()
    df_trades = feeder.get_latest_buys()
    if not df_trades.empty:
        logger.info(df_trades[['ticker', 'insider_name', 'value']].head())