"""
Unified notification system for Jupiter Sentinel.
Supports Telegram, Email (SMTP), and Webhooks with priority-based routing.
"""

import os
import json
import logging
import smtplib
import urllib.request
import urllib.parse
from email.message import EmailMessage
from enum import IntEnum
from typing import List, Optional

from .security import sanitize_sensitive_text
from .resilience import request_json

logger = logging.getLogger(__name__)

class Priority(IntEnum):
    """Notification priority levels."""
    INFO = 10      # Daily summary
    WARNING = 20   # Unusual activity
    CRITICAL = 30  # Stop-loss hit, large loss

class NotificationChannel:
    """Base class for notification channels."""
    
    def __init__(self, min_priority: Priority) -> None:
        self.min_priority = min_priority

    def send(self, message: str, priority: Priority, title: Optional[str] = None) -> bool:
        """Send a message if it meets the minimum priority."""
        if priority >= self.min_priority:
            return self._send_impl(message, priority, title)
        return False

    def _send_impl(self, message: str, priority: Priority, title: Optional[str] = None) -> bool:
        raise NotImplementedError()

class TelegramChannel(NotificationChannel):
    """Sends notifications to Telegram."""
    
    def __init__(self, min_priority: Priority) -> None:
        super().__init__(min_priority)
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None

    def _send_impl(self, message: str, priority: Priority, title: Optional[str] = None) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.debug("Telegram credentials not configured. Skipping.")
            return False

        try:
            url = f"{self.base_url}/sendMessage"
            
            prefix = ""
            if priority == Priority.INFO:
                prefix = "ℹ️ INFO"
            elif priority == Priority.WARNING:
                prefix = "⚠️ WARNING"
            elif priority == Priority.CRITICAL:
                prefix = "🚨 CRITICAL"
                
            full_text = f"<b>{prefix}</b>\n"
            if title:
                full_text += f"<b>{title}</b>\n"
            full_text += f"\n{message}"
            
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id, 
                "text": full_text, 
                "parse_mode": "HTML"
            }).encode("utf-8")
            
            req = urllib.request.Request(url, data=data)
            res = request_json(req, timeout=10, describe="Telegram sendMessage")
            
            if res and res.get("ok"):
                return True
            else:
                logger.error("Telegram API error: %s", sanitize_sensitive_text(res.get("description")))
                return False
        except Exception as e:
            logger.error("Error sending Telegram message: %s", sanitize_sensitive_text(str(e)))
            return False

class EmailChannel(NotificationChannel):
    """Sends notifications via SMTP."""
    
    def __init__(self, min_priority: Priority) -> None:
        super().__init__(min_priority)
        self.smtp_host = os.environ.get("SMTP_HOST")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER")
        self.smtp_pass = os.environ.get("SMTP_PASS")
        self.from_email = os.environ.get("EMAIL_FROM")
        self.to_email = os.environ.get("EMAIL_TO")

    def _send_impl(self, message: str, priority: Priority, title: Optional[str] = None) -> bool:
        if not all([self.smtp_host, self.smtp_user, self.smtp_pass, self.from_email, self.to_email]):
            logger.debug("SMTP credentials not fully configured. Skipping email.")
            return False
            
        try:
            msg = EmailMessage()
            msg.set_content(message)
            
            subject_prefix = f"[{priority.name}] "
            subject = subject_prefix + (title if title else "Jupiter Sentinel Alert")
            
            msg['Subject'] = subject
            msg['From'] = self.from_email
            msg['To'] = self.to_email
            
            with smtplib.SMTP(str(self.smtp_host), self.smtp_port) as server:
                server.starttls()
                server.login(str(self.smtp_user), str(self.smtp_pass))
                server.send_message(msg)
                
            return True
        except Exception as e:
            logger.error("Error sending Email: %s", sanitize_sensitive_text(str(e)))
            return False

class WebhookChannel(NotificationChannel):
    """Sends notifications to a custom webhook URL."""
    
    def __init__(self, min_priority: Priority) -> None:
        super().__init__(min_priority)
        self.webhook_url = os.environ.get("WEBHOOK_URL")

    def _send_impl(self, message: str, priority: Priority, title: Optional[str] = None) -> bool:
        if not self.webhook_url:
            logger.debug("Webhook URL not configured. Skipping webhook.")
            return False
            
        try:
            payload = {
                "priority": priority.name,
                "level": priority.value,
                "title": title or "Alert",
                "message": message
            }
            
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url, 
                data=data, 
                headers={"Content-Type": "application/json"}
            )
            
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status in (200, 201, 202, 204):
                    return True
                logger.error(f"Webhook returned status code: {response.status}")
                return False
        except Exception as e:
            logger.error("Error sending Webhook: %s", sanitize_sensitive_text(str(e)))
            return False

class NotificationManager:
    """Manages multiple notification channels and routes messages by priority."""
    
    def __init__(self) -> None:
        self.channels: List[NotificationChannel] = []
        self._configure_channels()
        
    def _configure_channels(self) -> None:
        """Configure channels based on environment variables."""
        tg_level = os.environ.get("NOTIFY_TELEGRAM_LEVEL", "INFO").upper()
        email_level = os.environ.get("NOTIFY_EMAIL_LEVEL", "CRITICAL").upper()
        webhook_level = os.environ.get("NOTIFY_WEBHOOK_LEVEL", "WARNING").upper()
        
        def parse_priority(level_str: str, default: Priority) -> Priority:
            try:
                return Priority[level_str]
            except KeyError:
                return default
                
        self.channels.append(TelegramChannel(parse_priority(tg_level, Priority.INFO)))
        self.channels.append(EmailChannel(parse_priority(email_level, Priority.CRITICAL)))
        self.channels.append(WebhookChannel(parse_priority(webhook_level, Priority.WARNING)))

    def send(self, message: str, priority: Priority, title: Optional[str] = None) -> None:
        """Dispatch message to all configured channels that meet the priority threshold."""
        for channel in self.channels:
            channel.send(message, priority, title)

    def info(self, message: str, title: Optional[str] = None) -> None:
        """Send an INFO level notification (e.g. daily summary)."""
        self.send(message, Priority.INFO, title)

    def warning(self, message: str, title: Optional[str] = None) -> None:
        """Send a WARNING level notification (e.g. unusual activity)."""
        self.send(message, Priority.WARNING, title)

    def critical(self, message: str, title: Optional[str] = None) -> None:
        """Send a CRITICAL level notification (e.g. stop-loss hit, large loss)."""
        self.send(message, Priority.CRITICAL, title)

# Global instance
notifier = NotificationManager()
