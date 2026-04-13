"""
Shared resilience helpers for API retries, fallback pricing, transaction
reconciliation, and durable JSON state files.
"""
from __future__ import annotations

import asyncio
import copy
import errno
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, TypeVar

from .config import RPC_URL, USDC_MINT

T = TypeVar("T")

MAX_API_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
PRICE_STALE_AFTER_SECONDS = 120.0
TX_RECONCILIATION_LOOKBACK_SECONDS = 3600.0

DEXSCREENER_BASE = "https://api.dexscreener.com"
TOKENS_BY_ADDRESS_URL = f"{DEXSCREENER_BASE}/tokens/v1"
DEXSCREENER_HEADERS = {
    "User-Agent": "JupiterSentinel/1.0",
    "Accept": "application/json",
}
SOLANA_CHAIN_ID = "solana"

DISK_FULL_ERRNOS = {errno.ENOSPC, errno.EDQUOT}
_MEMORY_ONLY_STATE_CACHE: dict[str, Any] = {}
_MEMORY_ONLY_WARNED_PATHS: set[str] = set()

_RETRYABLE_MESSAGE_FRAGMENTS = (
    "429",
    "connection aborted",
    "connection refused",
    "connection reset",
    "disk quota exceeded",
    "network is unreachable",
    "rate limit",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "too many requests",
)


@dataclass(frozen=True)
class ReconciledTransaction:
    signature: str
    status: str
    confirmation_status: str
    source: str
    slot: Optional[int] = None
    error: Optional[Any] = None


def _log(logger: Optional[Callable[[str], None]], message: str) -> None:
    if logger is not None:
        logger(message)


def _timestamp_suffix() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _path_key(path: Path | str) -> str:
    return str(Path(path).expanduser())


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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        try:
            return float(text)
        except ValueError:
            pass

        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None

    if isinstance(value, Mapping):
        return _extract_timestamp(value.get("timestamp"))

    return _extract_timestamp(getattr(value, "timestamp", None))


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
                        raise RuntimeError(
                            _http_error_message(
                                urllib.error.HTTPError(url, response.status, "", None, None),
                                parsed_payload,
                            )
                        )
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


def is_disk_full_error(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if exc.errno in DISK_FULL_ERRNOS:
        return True

    message = str(exc).lower()
    return "disk full" in message or "no space left" in message or "quota" in message


def get_memory_only_state(path: Path | str) -> Any:
    cached = _MEMORY_ONLY_STATE_CACHE.get(_path_key(path))
    return copy.deepcopy(cached)


def in_memory_only_mode(path: Path | str) -> bool:
    return _path_key(path) in _MEMORY_ONLY_STATE_CACHE


def _remember_memory_only_state(
    path: Path | str,
    payload: Any,
    *,
    logger: Optional[Callable[[str], None]] = None,
    error: Optional[BaseException] = None,
) -> None:
    key = _path_key(path)
    _MEMORY_ONLY_STATE_CACHE[key] = copy.deepcopy(payload)
    if key in _MEMORY_ONLY_WARNED_PATHS:
        return

    message = f"Disk full while writing {Path(path)}. Continuing in memory-only mode."
    if error is not None:
        message = f"{message} ({error})"
    warnings.warn(message, RuntimeWarning, stacklevel=2)
    _log(logger, message)
    _MEMORY_ONLY_WARNED_PATHS.add(key)


def _clear_memory_only_state(path: Path | str) -> None:
    key = _path_key(path)
    _MEMORY_ONLY_STATE_CACHE.pop(key, None)
    _MEMORY_ONLY_WARNED_PATHS.discard(key)


def read_json_file(path: Path | str) -> Any:
    candidate = Path(path)
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))

    cached = get_memory_only_state(candidate)
    if cached is not None:
        return cached

    raise FileNotFoundError(candidate)


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
    logger: Optional[Callable[[str], None]] = None,
) -> bool:
    target = Path(path)
    backup = Path(backup_path) if backup_path is not None else target.with_suffix(f"{target.suffix}.bak")

    serialized = json.dumps(payload, indent=2, sort_keys=True)
    try:
        atomic_write_text(target, serialized, encoding="utf-8")
        atomic_write_text(backup, serialized, encoding="utf-8")
    except OSError as exc:
        if not is_disk_full_error(exc):
            raise
        _remember_memory_only_state(target, payload, logger=logger, error=exc)
        return False

    _clear_memory_only_state(target)
    return True


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
        write_json_state(target, payload, backup_path=backup, logger=logger)
        _log(logger, f"Restored state from backup {backup}")
        return payload

    cached = get_memory_only_state(target)
    if cached is not None:
        _log(logger, f"Recovered in-memory state for {target}")
        return cached

    if default_factory is None:
        raise FileNotFoundError(f"No backup file exists for {target}")
    return default_factory()


def price_age_seconds(price_point: Any, *, now: Optional[float] = None) -> Optional[float]:
    timestamp = _extract_timestamp(price_point)
    if timestamp is None:
        return None

    current_time = time.time() if now is None else float(now)
    return max(current_time - timestamp, 0.0)


def is_price_stale(
    price_point: Any,
    *,
    max_age_seconds: float = PRICE_STALE_AFTER_SECONDS,
    now: Optional[float] = None,
) -> bool:
    age = price_age_seconds(price_point, now=now)
    if age is None:
        return True
    return age > float(max_age_seconds)


def prune_stale_price_history(
    history: Any,
    *,
    max_age_seconds: float = PRICE_STALE_AFTER_SECONDS,
    now: Optional[float] = None,
    logger: Optional[Callable[[str], None]] = None,
    pair_name: Optional[str] = None,
) -> bool:
    if not history:
        return False
    if not is_price_stale(history[-1], max_age_seconds=max_age_seconds, now=now):
        return False

    clear = getattr(history, "clear", None)
    if callable(clear):
        clear()
    else:
        del history[:]

    label = f" for {pair_name}" if pair_name else ""
    _log(
        logger,
        f"Discarded stale price history{label}; latest quote was older than {float(max_age_seconds):.0f}s.",
    )
    return True


def _fetch_dexscreener_pairs(
    token_address: str,
    *,
    timeout: float,
    logger: Optional[Callable[[str], None]] = None,
) -> Any:
    encoded = urllib.parse.quote(token_address.strip(), safe="")
    request = urllib.request.Request(
        f"{TOKENS_BY_ADDRESS_URL}/{SOLANA_CHAIN_ID}/{encoded}",
        headers=DEXSCREENER_HEADERS,
    )
    return request_json(request, timeout=timeout, logger=logger, describe="DexScreener token pairs")


def _candidate_price_from_pair(pair: Mapping[str, Any], input_mint: str, output_mint: str) -> Optional[tuple[float, int]]:
    base_token = pair.get("baseToken", {}) or {}
    quote_token = pair.get("quoteToken", {}) or {}
    base_address = str(base_token.get("address", "")).strip()
    quote_address = str(quote_token.get("address", "")).strip()
    price_native = _as_float(pair.get("priceNative"))
    raw_price_usd = pair.get("priceUsd")
    price_usd = None if raw_price_usd in (None, "") else _as_float(raw_price_usd)

    if base_address == input_mint and quote_address == output_mint:
        if output_mint == USDC_MINT and price_usd is not None and price_usd > 0:
            return price_usd, 2
        if price_native > 0:
            return price_native, 2
        return None

    if base_address == output_mint and quote_address == input_mint and price_native > 0:
        return 1.0 / price_native, 1

    return None


def fetch_dexscreener_price(
    input_mint: str,
    output_mint: str,
    *,
    timeout: float = 10.0,
    logger: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, Any]]:
    best: Optional[dict[str, Any]] = None

    for token_address in (input_mint, output_mint):
        try:
            payload = _fetch_dexscreener_pairs(token_address, timeout=timeout, logger=logger)
        except Exception as exc:
            _log(logger, f"DexScreener fallback failed for {token_address}: {exc}")
            continue

        if not isinstance(payload, list):
            continue

        for pair in payload:
            if not isinstance(pair, dict):
                continue
            if str(pair.get("chainId", "")).lower() != SOLANA_CHAIN_ID:
                continue

            candidate = _candidate_price_from_pair(pair, input_mint, output_mint)
            if candidate is None:
                continue

            price, direction_rank = candidate
            if price <= 0:
                continue

            liquidity_usd = _as_float((pair.get("liquidity", {}) or {}).get("usd"))
            volume_24h = _as_float((pair.get("volume", {}) or {}).get("h24"))
            scored = {
                "price": price,
                "source": "dexscreener",
                "pair_address": str(pair.get("pairAddress", "")).strip(),
                "pair_url": str(pair.get("url", "")).strip(),
                "liquidity_usd": liquidity_usd,
                "volume_24h": volume_24h,
                "_score": (direction_rank, liquidity_usd, volume_24h),
            }
            if best is None or scored["_score"] > best["_score"]:
                best = scored

    if best is None:
        return None

    best.pop("_score", None)
    return best


def rpc_request(
    method: str,
    params: Iterable[Any],
    *,
    rpc_url: str = RPC_URL,
    timeout: float = 10.0,
    logger: Optional[Callable[[str], None]] = None,
    describe: Optional[str] = None,
) -> Any:
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": list(params),
            }
        ).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = request_json(
        request,
        timeout=timeout,
        logger=logger,
        describe=describe or f"Solana {method}",
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected RPC response for {method}: {payload!r}")
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload.get("result")


def _payload_is_recent(payload: Mapping[str, Any], *, now: float, lookback_seconds: float) -> bool:
    timestamp = _extract_timestamp(payload.get("timestamp"))
    if timestamp is None:
        return True
    return max(now - timestamp, 0.0) <= float(lookback_seconds)


def _iter_transaction_payloads(
    node: Any,
    *,
    now: float,
    lookback_seconds: float,
    path: str = "state",
):
    if isinstance(node, dict):
        signature = node.get("tx_signature")
        if isinstance(signature, str) and signature.strip():
            status = str(node.get("status", "") or "").lower()
            if status != "dry_run" and _payload_is_recent(node, now=now, lookback_seconds=lookback_seconds):
                yield path, node

        for key, value in node.items():
            child_path = f"{path}.{key}"
            yield from _iter_transaction_payloads(value, now=now, lookback_seconds=lookback_seconds, path=child_path)
        return

    if isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_transaction_payloads(
                value,
                now=now,
                lookback_seconds=lookback_seconds,
                path=f"{path}[{index}]",
            )


def has_reconcilable_transactions(
    state: Mapping[str, Any],
    *,
    lookback_seconds: float = TX_RECONCILIATION_LOOKBACK_SECONDS,
    now: Optional[float] = None,
) -> bool:
    current_time = time.time() if now is None else float(now)
    return any(
        True
        for _path, _payload in _iter_transaction_payloads(
            state,
            now=current_time,
            lookback_seconds=lookback_seconds,
        )
    )


def get_signature_statuses(
    signatures: Iterable[str],
    *,
    rpc_url: str = RPC_URL,
    timeout: float = 10.0,
    logger: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    ordered = [signature for signature in signatures if signature]
    if not ordered:
        return {}

    result = rpc_request(
        "getSignatureStatuses",
        [ordered, {"searchTransactionHistory": True}],
        rpc_url=rpc_url,
        timeout=timeout,
        logger=logger,
        describe="Solana getSignatureStatuses",
    )
    values = []
    if isinstance(result, dict):
        values = result.get("value", [])
    return {signature: values[index] if index < len(values) else None for index, signature in enumerate(ordered)}


def _normalize_signature_status(signature: str, payload: Any, *, source: str) -> ReconciledTransaction:
    if not isinstance(payload, dict):
        return ReconciledTransaction(
            signature=signature,
            status="pending",
            confirmation_status="unknown",
            source=source,
        )

    error = payload.get("err")
    confirmation_status = str(payload.get("confirmationStatus", "") or "unknown")
    if error not in (None, ""):
        status = "failed"
    elif confirmation_status in {"confirmed", "finalized"}:
        status = "success"
    else:
        status = "pending"

    slot = payload.get("slot")
    return ReconciledTransaction(
        signature=signature,
        status=status,
        confirmation_status=confirmation_status,
        source=source,
        slot=int(slot) if slot is not None else None,
        error=error if error not in (None, "") else None,
    )


def reconcile_transaction_state(
    state: Mapping[str, Any],
    *,
    rpc_url: str = RPC_URL,
    timeout: float = 10.0,
    lookback_seconds: float = TX_RECONCILIATION_LOOKBACK_SECONDS,
    logger: Optional[Callable[[str], None]] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    updated_state = copy.deepcopy(dict(state))
    tracked_payloads = list(
        _iter_transaction_payloads(
            updated_state,
            now=current_time,
            lookback_seconds=lookback_seconds,
        )
    )

    signatures = list(dict.fromkeys(payload["tx_signature"] for _path, payload in tracked_payloads))
    statuses = get_signature_statuses(signatures, rpc_url=rpc_url, timeout=timeout, logger=logger)

    reconciled: list[ReconciledTransaction] = []
    changed = False
    reconciled_at = _utcnow()

    for path, payload in tracked_payloads:
        signature = str(payload["tx_signature"])
        tx_status = _normalize_signature_status(signature, statuses.get(signature), source=path)
        reconciled.append(tx_status)

        original_status = str(payload.get("status", "") or "")
        original_confirmation = str(payload.get("confirmation_status", "") or "")
        original_error = payload.get("error")

        payload["status"] = tx_status.status
        payload["confirmation_status"] = tx_status.confirmation_status
        payload["reconciled_at"] = reconciled_at
        if tx_status.slot is not None:
            payload["slot"] = tx_status.slot
        if tx_status.error is not None:
            payload["error"] = tx_status.error
        elif "error" in payload and tx_status.status == "success":
            payload.pop("error", None)

        if (
            original_status != tx_status.status
            or original_confirmation != tx_status.confirmation_status
            or original_error != payload.get("error")
        ):
            changed = True

    return {
        "state": updated_state,
        "transactions": reconciled,
        "changed": changed,
    }


__all__ = [
    "BASE_BACKOFF_SECONDS",
    "DEXSCREENER_BASE",
    "DEXSCREENER_HEADERS",
    "MAX_API_RETRIES",
    "MAX_BACKOFF_SECONDS",
    "PRICE_STALE_AFTER_SECONDS",
    "RETRYABLE_HTTP_STATUS_CODES",
    "ReconciledTransaction",
    "TX_RECONCILIATION_LOOKBACK_SECONDS",
    "archive_corrupt_file",
    "async_request_json",
    "atomic_write_text",
    "call_with_retry",
    "fetch_dexscreener_price",
    "get_memory_only_state",
    "get_signature_statuses",
    "has_reconcilable_transactions",
    "in_memory_only_mode",
    "is_disk_full_error",
    "is_price_stale",
    "is_retryable_exception",
    "price_age_seconds",
    "prune_stale_price_history",
    "read_json_file",
    "reconcile_transaction_state",
    "request_json",
    "restore_json_from_backup",
    "rpc_request",
    "write_json_state",
]
