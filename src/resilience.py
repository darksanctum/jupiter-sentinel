"""
Shared resilience helpers for API retries and durable JSON state files.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TypeVar

T = TypeVar("T")

MAX_API_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_RETRYABLE_MESSAGE_FRAGMENTS = (
    "429",
    "connection aborted",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "rate limit",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "too many requests",
)


def _log(logger: Optional[Callable[[str], None]], message: str) -> None:
    if logger is not None:
        logger(message)


def _timestamp_suffix() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")


def _compute_backoff_delay(attempt: int, *, retry_after: Optional[float] = None) -> float:
    if retry_after is not None:
        return max(0.0, min(float(retry_after), MAX_BACKOFF_SECONDS))
    return min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)


def _parse_retry_after(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        return max(float(value), 0.0)

    text = str(value).strip()
    if not text:
        return None

    try:
        return max(float(text), 0.0)
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None

    if parsed.tzinfo is not None:
        delta = parsed.timestamp() - time.time()
    else:
        delta = (parsed - datetime.utcnow()).total_seconds()
    return max(delta, 0.0)


def _extract_retry_after_from_payload(payload: Any) -> Optional[float]:
    if not isinstance(payload, dict):
        return None

    parameters = payload.get("parameters")
    if isinstance(parameters, dict):
        retry_after = _parse_retry_after(parameters.get("retry_after"))
        if retry_after is not None:
            return retry_after

    return _parse_retry_after(payload.get("retry_after"))


def _read_http_error_payload(exc: urllib.error.HTTPError) -> tuple[bytes, Optional[Any]]:
    try:
        raw = exc.read()
    except Exception:
        raw = b""

    if not raw:
        return raw, None

    try:
        return raw, json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw, None


def _http_error_message(exc: urllib.error.HTTPError, payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("error", "message", "description"):
            value = payload.get(key)
            if isinstance(value, dict):
                nested = value.get("message")
                if nested:
                    return str(nested)
            elif value not in (None, ""):
                return str(value)
    return str(exc)


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, ssl.SSLError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True

    message = str(exc).lower()
    return any(fragment in message for fragment in _RETRYABLE_MESSAGE_FRAGMENTS)


def call_with_retry(
    operation: Callable[[], T],
    *,
    max_retries: int = MAX_API_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    logger: Optional[Callable[[str], None]] = None,
    describe: str = "operation",
    is_retryable: Optional[Callable[[Exception], bool]] = None,
) -> T:
    retry_checker = is_retryable or is_retryable_exception
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries or not retry_checker(exc):
                raise

            delay = _compute_backoff_delay(attempt)
            _log(logger, f"{describe} failed: {exc}. Retrying in {delay:.1f}s.")
            sleep_fn(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{describe} failed without an exception")


def request_json(
    request_or_url: urllib.request.Request | str,
    *,
    timeout: float,
    headers: Optional[Mapping[str, str]] = None,
    max_retries: int = MAX_API_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    logger: Optional[Callable[[str], None]] = None,
    describe: str = "HTTP request",
) -> Any:
    request = (
        request_or_url
        if isinstance(request_or_url, urllib.request.Request)
        else urllib.request.Request(request_or_url, headers=dict(headers or {}))
    )

    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
            return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            last_error = exc
            raw_payload, parsed_payload = _read_http_error_payload(exc)
            retry_after = _parse_retry_after(exc.headers.get("Retry-After")) if exc.headers else None
            if retry_after is None:
                retry_after = _extract_retry_after_from_payload(parsed_payload)

            if exc.code in RETRYABLE_HTTP_STATUS_CODES and attempt < max_retries:
                delay = _compute_backoff_delay(attempt, retry_after=retry_after)
                _log(
                    logger,
                    f"{describe} returned HTTP {exc.code}. Retrying in {delay:.1f}s.",
                )
                sleep_fn(delay)
                continue

            if parsed_payload is not None:
                raise RuntimeError(_http_error_message(exc, parsed_payload)) from exc
            if raw_payload:
                raise RuntimeError(raw_payload.decode("utf-8", errors="replace")) from exc
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = exc
            if attempt >= max_retries:
                raise

            delay = _compute_backoff_delay(attempt)
            _log(logger, f"{describe} returned invalid JSON. Retrying in {delay:.1f}s.")
            sleep_fn(delay)
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries or not is_retryable_exception(exc):
                raise

            delay = _compute_backoff_delay(attempt)
            _log(logger, f"{describe} failed: {exc}. Retrying in {delay:.1f}s.")
            sleep_fn(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{describe} failed without an exception")


async def async_request_json(
    session: Any,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 10.0,
    max_retries: int = MAX_API_RETRIES,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
    logger: Optional[Callable[[str], None]] = None,
    describe: str = "HTTP request",
) -> Any:
    import aiohttp

    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            async with session.get(url, params=params, headers=dict(headers or {}), timeout=timeout) as response:
                raw_text = await response.text()

                if response.status >= 400:
                    parsed_payload = None
                    try:
                        parsed_payload = json.loads(raw_text)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed_payload = None

                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    if retry_after is None:
                        retry_after = _extract_retry_after_from_payload(parsed_payload)

                    if response.status in RETRYABLE_HTTP_STATUS_CODES and attempt < max_retries:
                        delay = _compute_backoff_delay(attempt, retry_after=retry_after)
                        _log(
                            logger,
                            f"{describe} returned HTTP {response.status}. Retrying in {delay:.1f}s.",
                        )
                        await sleep_fn(delay)
                        continue

                    if parsed_payload is not None:
                        raise RuntimeError(_http_error_message(urllib.error.HTTPError(url, response.status, "", None, None), parsed_payload))
                    raise RuntimeError(raw_text or f"HTTP {response.status}")

                try:
                    return json.loads(raw_text)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt >= max_retries:
                        raise

                    delay = _compute_backoff_delay(attempt)
                    _log(logger, f"{describe} returned invalid JSON. Retrying in {delay:.1f}s.")
                    await sleep_fn(delay)
                    continue
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= max_retries or not is_retryable_exception(exc):
                raise

            delay = _compute_backoff_delay(attempt)
            _log(logger, f"{describe} failed: {exc}. Retrying in {delay:.1f}s.")
            await sleep_fn(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{describe} failed without an exception")


def archive_corrupt_file(
    path: Path | str,
    *,
    logger: Optional[Callable[[str], None]] = None,
) -> Optional[Path]:
    candidate = Path(path)
    if not candidate.exists():
        return None

    archived = candidate.with_name(f"{candidate.name}.corrupt-{_timestamp_suffix()}")
    candidate.replace(archived)
    _log(logger, f"Archived corrupt state file to {archived}")
    return archived


def read_json_file(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return

    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return

    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def atomic_write_text(
    path: Path | str,
    payload: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    target_tmp = target.with_name(f".{target.name}.{_timestamp_suffix()}.tmp")
    with target_tmp.open("w", encoding=encoding) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(target_tmp, target)
    _fsync_directory(target.parent)
    return target


def write_json_state(
    path: Path | str,
    payload: Any,
    *,
    backup_path: Optional[Path | str] = None,
) -> None:
    target = Path(path)
    backup = Path(backup_path) if backup_path is not None else target.with_suffix(f"{target.suffix}.bak")

    serialized = json.dumps(payload, indent=2, sort_keys=True)
    atomic_write_text(target, serialized, encoding="utf-8")
    atomic_write_text(backup, serialized, encoding="utf-8")


def restore_json_from_backup(
    path: Path | str,
    *,
    backup_path: Optional[Path | str] = None,
    default_factory: Optional[Callable[[], T]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> T:
    target = Path(path)
    backup = Path(backup_path) if backup_path is not None else target.with_suffix(f"{target.suffix}.bak")

    if backup.exists():
        payload = read_json_file(backup)
        write_json_state(target, payload, backup_path=backup)
        _log(logger, f"Restored state from backup {backup}")
        return payload

    if default_factory is None:
        raise FileNotFoundError(f"No backup file exists for {target}")
    return default_factory()
