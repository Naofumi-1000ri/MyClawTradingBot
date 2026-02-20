"""Telegram notification for myClaw."""

import os
import time

import requests

from src.utils.logger import setup_logger

logger = setup_logger("telegram")

# リトライ設定
_MAX_RETRIES = 2
_RETRY_DELAY = 5.0  # 秒


def send_message(text: str) -> bool:
    """Send a message via Telegram Bot API.

    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
    If not set, logs the message instead of raising an error.

    ネットワークエラー時は最大 _MAX_RETRIES 回リトライする。
    APIエラー (4xx/5xx) はリトライしない。

    Returns:
        True: 送信成功, False: 設定なし or 送信失敗
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.info("Telegram not configured. Message: %s", text)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                if attempt > 0:
                    logger.info("Telegram: sent on attempt %d", attempt + 1)
                return True
            else:
                # APIエラー (4xx/5xx) はリトライしない
                logger.warning(
                    "Telegram API error %d: %s", resp.status_code, resp.text[:200]
                )
                return False
        except requests.exceptions.Timeout:
            err_type = "timeout"
        except requests.exceptions.ConnectionError as e:
            err_type = f"connection error: {e}"
        except Exception as e:
            logger.warning("Failed to send Telegram message: %s", e)
            return False

        remaining = _MAX_RETRIES - attempt
        if remaining > 0:
            logger.warning(
                "Telegram send %s (attempt %d/%d). Retrying in %.0fs...",
                err_type, attempt + 1, _MAX_RETRIES + 1, _RETRY_DELAY,
            )
            time.sleep(_RETRY_DELAY)
        else:
            logger.warning(
                "Telegram send failed after %d attempts (%s). Giving up.",
                _MAX_RETRIES + 1, err_type,
            )

    return False
