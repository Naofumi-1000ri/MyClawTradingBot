"""Telegram notification for myClaw."""

import os

import requests

from src.utils.logger import setup_logger

logger = setup_logger("telegram")


def send_message(text: str) -> None:
    """Send a message via Telegram Bot API.

    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
    If not set, logs the message instead of raising an error.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.info("Telegram not configured. Message: %s", text)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning("Telegram API error %d: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Failed to send Telegram message: %s", e)
