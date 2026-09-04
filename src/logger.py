import logging
from logging.handlers import RotatingFileHandler
import os

if not os.path.exists('logs'):
    os.makedirs('logs')

def get_logger(name='InsiderBot'):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # no duplication during imports
    if not logger.handlers:
        # Formats: Date | Level | Modul | Message
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(module)-12s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # stdout
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # Rotational file handling
        file_handler = RotatingFileHandler(
            'logs/trading_bot.log', 
            maxBytes=5*1024*1024, 
            backupCount=3, 
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger

# public object
logger = get_logger()