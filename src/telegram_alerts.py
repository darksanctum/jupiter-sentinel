import os
import urllib.request
import urllib.parse
import time
import logging
from threading import Lock

from .resilience import request_json
from .security import sanitize_sensitive_text

logger = logging.getLogger(__name__)

class TelegramAlerter:
    """
    Sends trade alerts to Telegram.
    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
    """
    
    def __init__(self):
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        
        # Rate limiting state
        self.last_sent_time = 0.0
        self.lock = Lock()
        self.rate_limit_delay = 1.0  # 1 message per second to avoid 429 Too Many Requests

    def _send_message(self, text: str) -> bool:
        """Internal method to send message with rate limiting."""
        if not self.bot_token or not self.chat_id:
            logger.debug("Telegram credentials not configured. Skipping alert.")
            return False

        with self.lock:
            now = time.time()
            elapsed = now - self.last_sent_time
            if elapsed < self.rate_limit_delay:
                time.sleep(self.rate_limit_delay - elapsed)
            
            url = f"{self.base_url}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }).encode("utf-8")
            
            req = urllib.request.Request(url, data=data)
            
            try:
                res = request_json(req, timeout=10, describe="Telegram sendMessage")
                if res.get("ok"):
                    self.last_sent_time = time.time()
                    return True
                logger.error("Failed to send Telegram message: %s", sanitize_sensitive_text(res.get("description")))
                return False
            except Exception as e:
                logger.error("Error sending Telegram message: %s", sanitize_sensitive_text(e))
                return False

    def alert_position_opened(self, symbol: str, side: str, amount: float, price: float):
        msg = (
            f"🟢 <b>Position Opened</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Amount: {amount}\n"
            f"Entry Price: ${price:,.4f}"
        )
        return self._send_message(msg)

    def alert_position_closed(self, symbol: str, side: str, amount: float, price: float, pnl: float):
        icon = "💰" if pnl >= 0 else "💸"
        msg = (
            f"🔴 <b>Position Closed</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Amount: {amount}\n"
            f"Exit Price: ${price:,.4f}\n"
            f"P&L: {icon} ${pnl:,.4f}"
        )
        return self._send_message(msg)

    def alert_stop_loss(self, symbol: str, side: str, amount: float, price: float, pnl: float):
        msg = (
            f"🛑 <b>Stop-Loss Triggered</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Amount: {amount}\n"
            f"Exit Price: ${price:,.4f}\n"
            f"P&L: 💸 ${pnl:,.4f}"
        )
        return self._send_message(msg)

    def alert_take_profit(self, symbol: str, side: str, amount: float, price: float, pnl: float):
        msg = (
            f"🎯 <b>Take-Profit Hit</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Amount: {amount}\n"
            f"Exit Price: ${price:,.4f}\n"
            f"P&L: 💰 ${pnl:,.4f}"
        )
        return self._send_message(msg)

    def alert_error(self, error_msg: str):
        msg = (
            f"⚠️ <b>Error Occurred</b>\n"
            f"<pre>{sanitize_sensitive_text(error_msg)}</pre>"
        )
        return self._send_message(msg)
