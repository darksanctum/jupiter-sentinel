"""Module explaining what this file does."""

from typing import Any
import os
import time
import json
import logging
from pathlib import Path
from datetime import datetime
from threading import Lock

from .telegram_alerts import TelegramAlerter
from .config import DATA_DIR

logger = logging.getLogger(__name__)

HEALTH_DIR = DATA_DIR / "health"
HEALTH_DIR.mkdir(parents=True, exist_ok=True)


class HealthMonitor:
    """
    Health check system that monitors:
    - API response times
    - trade execution times
    - wallet balance changes
    - error rates
    - uptime
    """

    def __init__(self) -> None:
        """Function docstring."""
        self.start_time = time.time()
        self.lock = Lock()
        self.alerter = TelegramAlerter()

        # Metrics
        self.api_response_times = []
        self.trade_execution_times = []
        self.last_balance = None
        self.initial_balance = None

        self.total_requests = 0
        self.total_errors = 0

        self.last_successful_api_call = time.time()
        self.last_report_time = time.time()

    def record_api_response(
        self, endpoint: str, response_time_ms: float, success: bool = True
    ) -> Any:
        """Function docstring."""
        with self.lock:
            self.total_requests += 1
            if success:
                self.api_response_times.append(response_time_ms)
                self.last_successful_api_call = time.time()
            else:
                self.total_errors += 1

            # Keep last 1000 response times to prevent memory growth
            if len(self.api_response_times) > 1000:
                self.api_response_times.pop(0)

    def record_trade_execution(self, symbol: str, execution_time_ms: float) -> Any:
        """Function docstring."""
        with self.lock:
            self.trade_execution_times.append(execution_time_ms)
            if len(self.trade_execution_times) > 1000:
                self.trade_execution_times.pop(0)

    def record_wallet_balance(self, balance: float) -> Any:
        """Function docstring."""
        with self.lock:
            if self.initial_balance is None:
                self.initial_balance = balance

            if self.last_balance is not None:
                # Check for unexpected drop (>10% drop)
                drop_pct = (
                    (self.last_balance - balance) / self.last_balance
                    if self.last_balance > 0
                    else 0
                )
                if drop_pct > 0.10:
                    msg = (
                        f"⚠️ <b>Wallet Balance Dropped Unexpectedly</b>\n"
                        f"Previous: {self.last_balance:,.4f}\n"
                        f"Current: {balance:,.4f}\n"
                        f"Drop: {drop_pct:.1%}"
                    )
                    self.alerter._send_message(msg)
                    logger.warning(
                        f"Wallet balance dropped unexpectedly: {drop_pct:.1%}"
                    )

            self.last_balance = balance

    def record_error(self, error_type: str = "Unknown") -> Any:
        """Function docstring."""
        with self.lock:
            self.total_requests += 1
            self.total_errors += 1

    def tick(self) -> Any:
        """
        Evaluate current health metrics and send alerts if needed.
        Should be called periodically (e.g., every minute) by the main loop.
        """
        with self.lock:
            now = time.time()

            # Check API down > 5 min
            api_downtime = now - self.last_successful_api_call
            if api_downtime > 300:
                msg = (
                    f"🚨 <b>API DOWN</b>\n"
                    f"No successful API calls for {api_downtime / 60:.1f} minutes."
                )
                self.alerter._send_message(msg)
                logger.error(f"API down > 5 min ({api_downtime / 60:.1f} min)")
                # Reset to avoid spamming every tick, alert again in 5 mins
                self.last_successful_api_call = now

            # Check Error rate > 10%
            # Require a minimum sample size (e.g., 20 requests) before alerting
            if self.total_requests >= 20:
                error_rate = self.total_errors / self.total_requests
                if error_rate > 0.10:
                    msg = (
                        f"🚨 <b>High Error Rate</b>\n"
                        f"Error rate is currently {error_rate:.1%}\n"
                        f"Total requests: {self.total_requests}\n"
                        f"Errors: {self.total_errors}"
                    )
                    self.alerter._send_message(msg)
                    logger.error(f"High error rate: {error_rate:.1%}")
                    # Reset counters after alerting to monitor the next window
                    self.total_requests = 0
                    self.total_errors = 0

            # Generate hourly report
            if now - self.last_report_time >= 3600:
                self._generate_hourly_report(now)
                self.last_report_time = now

    def _generate_hourly_report(self, now_timestamp: float) -> Any:
        """Function docstring."""
        uptime_hrs = (now_timestamp - self.start_time) / 3600

        avg_api_time = (
            (sum(self.api_response_times) / len(self.api_response_times))
            if self.api_response_times
            else 0.0
        )
        avg_trade_time = (
            (sum(self.trade_execution_times) / len(self.trade_execution_times))
            if self.trade_execution_times
            else 0.0
        )
        error_rate = (
            (self.total_errors / self.total_requests)
            if self.total_requests > 0
            else 0.0
        )

        report = {
            "timestamp": datetime.fromtimestamp(now_timestamp).isoformat(),
            "uptime_hours": round(uptime_hrs, 2),
            "metrics": {
                "avg_api_response_ms": round(avg_api_time, 2),
                "avg_trade_execution_ms": round(avg_trade_time, 2),
                "error_rate": round(error_rate, 4),
                "total_requests": self.total_requests,
                "total_errors": self.total_errors,
            },
            "wallet": {
                "initial_balance": self.initial_balance,
                "current_balance": self.last_balance,
            },
        }

        # Save to data/health/
        report_filename = f"health_report_{int(now_timestamp)}.json"
        report_path = HEALTH_DIR / report_filename

        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            logger.info(f"Hourly health report saved to {report_path}")
        except Exception as e:
            logger.error(f"Failed to save health report: {e}")

        # Reset windowed metrics for the next hour
        self.api_response_times.clear()
        self.trade_execution_times.clear()


# Global singleton instance
monitor = HealthMonitor()
