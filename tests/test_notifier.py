import pytest
from unittest.mock import patch, MagicMock
from notifier import TelegramNotifier

@patch('notifier.requests.post')
def test_telegram_notifier_success(mock_post):
    """Test that Telegram notifications send successfully via HTTP API."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    with patch('notifier.config') as mock_conf:
        mock_conf.telegram_token = "fake_token"
        mock_conf.telegram_chat = "fake_chat"
        
        notifier = TelegramNotifier()
        notifier.send_message("Test alert")
        
        mock_post.assert_called_once()