"""
Lightweight HTTP healthcheck server for the long-running trading service.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Optional, Protocol


logger = logging.getLogger(__name__)
DEFAULT_HEALTH_PATH = "/healthz"


class HealthProvider(Protocol):
    """Contract for objects that can produce service health snapshots."""

    def get_health_snapshot(
        self, stale_after_secs: float = 180.0
    ) -> tuple[int, dict[str, Any]]:
        """Return an HTTP status code and JSON payload."""


class _HealthHandler(BaseHTTPRequestHandler):
    server: "_HealthHTTPServer"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != self.server.health_path:
            self.send_error(404, "Not Found")
            return

        status_code, payload = self.server.provider.get_health_snapshot(
            self.server.stale_after_secs
        )
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("healthcheck %s", format % args)


class _HealthHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        host: str,
        port: int,
        *,
        provider: HealthProvider,
        stale_after_secs: float,
        health_path: str,
    ) -> None:
        super().__init__((host, port), _HealthHandler)
        self.provider = provider
        self.stale_after_secs = stale_after_secs
        self.health_path = health_path


class BotHealthServer:
    """Run a minimal HTTP server on a background thread for container healthchecks."""

    def __init__(
        self,
        provider: HealthProvider,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        stale_after_secs: float = 180.0,
        health_path: str = DEFAULT_HEALTH_PATH,
    ) -> None:
        self._server = _HealthHTTPServer(
            host,
            port,
            provider=provider,
            stale_after_secs=stale_after_secs,
            health_path=health_path,
        )
        self._thread: Optional[Thread] = None

    @property
    def port(self) -> int:
        """Return the bound TCP port."""
        return int(self._server.server_address[1])

    @property
    def health_path(self) -> str:
        """Return the HTTP path served by the health server."""
        return str(self._server.health_path)

    def start(self) -> None:
        """Start serving health responses in a daemon thread."""
        if self._thread is not None:
            return

        self._thread = Thread(
            target=self._server.serve_forever,
            name="jupiter-sentinel-health",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background HTTP server."""
        if self._thread is None:
            return

        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
        self._thread = None
