import requests
from config import config
from logger import logger

class TelegramNotifier:
    def __init__(self):
        self.token = config.telegram_token
        self.chat_id = config.telegram_chatid
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, text: str) -> None:
        """Sends a message via the Telegram API."""
        if not self.token or not self.chat_id:
            logger.warning("Telegram Notifier: Token or Chat ID is not configured.")
            return

        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code != 200:
                logger.error(f"Telegram API Error: {response.text}")
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}", exc_info=True)

# --- TESTING ---
if __name__ == "__main__":
    notifier = TelegramNotifier()
    notifier.send_message("<b>Test Message!</b> The bot has successfully connected to Telegram.")