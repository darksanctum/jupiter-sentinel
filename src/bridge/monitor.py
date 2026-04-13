"""
Monitor pending cross-chain bridge transfers and alert when they complete.

This module tracks transfers across:
- Wormhole (via Wormholescan operations)
- deBridge / DLN
- Mayan

Provider status APIs are normalized into a small common model so the caller can
poll pending transfers and react as soon as funds arrive on the destination
chain.
"""

from __future__ import annotations

import logging
import math
import os
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

from ..config import DATA_DIR, HEADERS
from ..notifications import notifier
from ..resilience import read_json_file, request_json, write_json_state

logger = logging.getLogger(__name__)

BridgeName = Literal["wormhole", "debridge", "mayan"]
NormalizedBridgeStatus = Literal[
    "pending",
    "completed",
    "failed",
    "cancelled",
    "unknown",
]

BRIDGE_DIR = DATA_DIR / "bridge"
BRIDGE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_STATE_PATH = BRIDGE_DIR / "pending_transfers.json"
DEFAULT_HTTP_HEADERS = {
    "Accept": "application/json",
    "User-Agent": HEADERS.get("User-Agent", "JupiterSentinel/1.0"),
}

WORMHOLESCAN_BASE_URL = os.environ.get(
    "WORMHOLESCAN_BASE_URL", "https://api.wormholescan.io/api/v1"
).rstrip("/")
DEBRIDGE_STATS_BASE_URL = os.environ.get(
    "DEBRIDGE_STATS_BASE_URL", "https://stats-api.dln.trade/api"
).rstrip("/")
MAYAN_EXPLORER_BASE_URL = os.environ.get(
    "MAYAN_EXPLORER_BASE_URL", "https://explorer-api.mayan.finance/v3"
).rstrip("/")

DEBRIDGE_SUCCESS_STATUSES = {"Fulfilled", "SentUnlock", "ClaimedUnlock"}
DEBRIDGE_CANCELLED_STATUSES = {
    "OrderCancelled",
    "SentOrderCancel",
    "ClaimedOrderCancel",
}
FAILURE_STATUS_FRAGMENTS = ("FAIL", "ERROR", "REFUND", "REVERT", "EXPIRE")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _safe_str(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _build_url(base_url: str, path: str, params: Optional[dict[str, Any]] = None) -> str:
    base = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if not params:
        return base
    encoded = urllib.parse.urlencode(
        {
            key: value
            for key, value in params.items()
            if value not in (None, "")
        }
    )
    return f"{base}?{encoded}" if encoded else base


def _deep_get(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _estimate_completion(
    *,
    started_at: Optional[str],
    expected_completion_seconds: Optional[int],
) -> tuple[Optional[str], Optional[int]]:
    if expected_completion_seconds is None:
        return None, None

    started = _parse_datetime(started_at)
    if started is None:
        return None, None

    eta = started + timedelta(seconds=max(expected_completion_seconds, 0))
    remaining = max(math.ceil((eta - _utcnow()).total_seconds()), 0)
    return _isoformat(eta), remaining


def _order_id_string(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return _safe_str(value.get("stringValue")) or _safe_str(value.get("bytesValue"))
    return _safe_str(value)


def _normalize_debridge_status(raw_status: Optional[str]) -> NormalizedBridgeStatus:
    if raw_status in DEBRIDGE_SUCCESS_STATUSES:
        return "completed"
    if raw_status in DEBRIDGE_CANCELLED_STATUSES:
        return "cancelled"

    upper = (raw_status or "").upper()
    if not upper:
        return "unknown"
    if upper == "CREATED":
        return "pending"
    if any(fragment in upper for fragment in FAILURE_STATUS_FRAGMENTS):
        return "failed"
    return "pending"


def _normalize_mayan_status(payload: dict[str, Any]) -> NormalizedBridgeStatus:
    raw_status = _safe_str(payload.get("status")) or "UNKNOWN"
    upper = raw_status.upper()

    if payload.get("completedAt") or any(
        fragment in upper for fragment in ("SETTLED", "COMPLETED", "FULFILLED")
    ):
        return "completed"
    if "CANCEL" in upper:
        return "cancelled"
    if any(fragment in upper for fragment in FAILURE_STATUS_FRAGMENTS):
        return "failed"
    return "pending"


def _normalize_wormhole_status(operation: dict[str, Any]) -> NormalizedBridgeStatus:
    if operation.get("targetChain"):
        return "completed"

    source_status = (
        _safe_str(_deep_get(operation, "sourceChain", "status")) or ""
    ).upper()
    if any(fragment in source_status for fragment in FAILURE_STATUS_FRAGMENTS):
        return "failed"

    if operation.get("sourceChain") or operation.get("vaa"):
        return "pending"
    return "unknown"


def _extract_debridge_destination_tx_hash(payload: dict[str, Any]) -> Optional[str]:
    candidates = (
        ("fulfilledDstEventMetadata", "transactionHash"),
        ("fulfilledDstEventMetadata", "txHash"),
        ("fulfillTx", "hash"),
        ("fulfillTx", "txHash"),
        ("execution", "txHash"),
    )
    for path in candidates:
        value = _safe_str(_deep_get(payload, *path))
        if value:
            return value
    return _safe_str(payload.get("destinationTxHash")) or _safe_str(
        payload.get("fulfillTxHash")
    )


def _extract_mayan_destination_tx_hash(payload: dict[str, Any]) -> Optional[str]:
    candidates = (
        ("destinationTxHash",),
        ("destTxHash",),
        ("swapTxHash",),
        ("settleTxHash",),
        ("releaseTxHash",),
    )
    for path in candidates:
        value = _safe_str(_deep_get(payload, *path))
        if value:
            return value
    return None


@dataclass(slots=True)
class BridgeTransfer:
    bridge: BridgeName
    tracking_id: str
    created_at: str = field(default_factory=lambda: _isoformat(_utcnow()))
    status: NormalizedBridgeStatus = "pending"
    raw_status: Optional[str] = None
    last_checked_at: Optional[str] = None
    completed_at: Optional[str] = None
    destination_tx_hash: Optional[str] = None
    completion_alert_sent: bool = False
    last_error: Optional[str] = None
    expected_completion_seconds: Optional[int] = None
    source_tx_hash: Optional[str] = None
    creation_tx_hash: Optional[str] = None
    order_id: Optional[str] = None
    emitter_address: Optional[str] = None
    operation_id: Optional[str] = None
    sequence: Optional[str] = None
    source_chain: Optional[str] = None
    destination_chain: Optional[str] = None
    destination_address: Optional[str] = None
    asset_in_symbol: Optional[str] = None
    asset_out_symbol: Optional[str] = None
    amount_in: Optional[str] = None
    amount_out: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.bridge}:{self.tracking_id}"

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BridgeTransfer":
        return cls(**payload)

    @classmethod
    def wormhole(
        cls,
        *,
        emitter_address: str,
        source_tx_hash: Optional[str] = None,
        operation_id: Optional[str] = None,
        sequence: Optional[str] = None,
        expected_completion_seconds: Optional[int] = None,
        **kwargs: Any,
    ) -> "BridgeTransfer":
        if not any((source_tx_hash, operation_id, sequence)):
            raise ValueError(
                "Wormhole tracking requires source_tx_hash, operation_id, or sequence."
            )
        tracking_id = (
            _safe_str(operation_id)
            or _safe_str(source_tx_hash)
            or _safe_str(sequence)
        )
        assert tracking_id is not None
        return cls(
            bridge="wormhole",
            tracking_id=tracking_id,
            emitter_address=emitter_address,
            source_tx_hash=source_tx_hash,
            operation_id=operation_id,
            sequence=sequence,
            expected_completion_seconds=expected_completion_seconds,
            **kwargs,
        )

    @classmethod
    def debridge(
        cls,
        *,
        order_id: Optional[str] = None,
        creation_tx_hash: Optional[str] = None,
        approximate_fulfillment_delay: Optional[int] = None,
        **kwargs: Any,
    ) -> "BridgeTransfer":
        tracking_id = _safe_str(order_id) or _safe_str(creation_tx_hash)
        if tracking_id is None:
            raise ValueError("deBridge tracking requires order_id or creation_tx_hash.")
        return cls(
            bridge="debridge",
            tracking_id=tracking_id,
            order_id=order_id,
            creation_tx_hash=creation_tx_hash,
            source_tx_hash=creation_tx_hash,
            expected_completion_seconds=approximate_fulfillment_delay,
            **kwargs,
        )

    @classmethod
    def mayan(
        cls,
        *,
        source_tx_hash: str,
        eta_seconds: Optional[int] = None,
        **kwargs: Any,
    ) -> "BridgeTransfer":
        if not _safe_str(source_tx_hash):
            raise ValueError("Mayan tracking requires source_tx_hash.")
        return cls(
            bridge="mayan",
            tracking_id=source_tx_hash,
            source_tx_hash=source_tx_hash,
            expected_completion_seconds=eta_seconds,
            **kwargs,
        )


@dataclass(frozen=True, slots=True)
class BridgeTransferStatus:
    bridge: BridgeName
    tracking_id: str
    status: NormalizedBridgeStatus
    raw_status: Optional[str] = None
    source_tx_hash: Optional[str] = None
    destination_tx_hash: Optional[str] = None
    completed_at: Optional[str] = None
    estimated_completion_time: Optional[str] = None
    estimated_seconds_remaining: Optional[int] = None
    details: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


CompletionHandler = Callable[[BridgeTransfer, BridgeTransferStatus], None]


class BridgeMonitor:
    """Track pending bridge transfers and send one-shot completion alerts."""

    def __init__(
        self,
        *,
        state_path: Path | str = DEFAULT_STATE_PATH,
        poll_timeout: float = 10.0,
        on_completion: Optional[CompletionHandler] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.poll_timeout = poll_timeout
        self.on_completion = on_completion
        self.sleep_fn = sleep_fn
        self.transfers: dict[str, BridgeTransfer] = {}
        self._load_state()

    def _load_state(self) -> None:
        try:
            payload = read_json_file(self.state_path)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("Failed to read bridge state %s: %s", self.state_path, exc)
            return

        raw_transfers = payload.get("transfers", []) if isinstance(payload, dict) else []
        for raw_transfer in raw_transfers:
            try:
                transfer = BridgeTransfer.from_dict(raw_transfer)
            except TypeError as exc:
                logger.warning("Skipping invalid bridge transfer state entry: %s", exc)
                continue
            self.transfers[transfer.key] = transfer

    def _save_state(self) -> None:
        write_json_state(
            self.state_path,
            {"transfers": [transfer.to_dict() for transfer in self.transfers.values()]},
            logger=logger.warning,
        )

    def track_transfer(self, transfer: BridgeTransfer) -> BridgeTransfer:
        self.transfers[transfer.key] = transfer
        self._save_state()
        return transfer

    def track_wormhole_transfer(self, **kwargs: Any) -> BridgeTransfer:
        return self.track_transfer(BridgeTransfer.wormhole(**kwargs))

    def track_debridge_transfer(self, **kwargs: Any) -> BridgeTransfer:
        return self.track_transfer(BridgeTransfer.debridge(**kwargs))

    def track_mayan_transfer(self, **kwargs: Any) -> BridgeTransfer:
        return self.track_transfer(BridgeTransfer.mayan(**kwargs))

    def forget_transfer(self, bridge: BridgeName, tracking_id: str) -> bool:
        removed = self.transfers.pop(f"{bridge}:{tracking_id}", None)
        if removed is None:
            return False
        self._save_state()
        return True

    def list_transfers(self, *, include_terminal: bool = True) -> list[BridgeTransfer]:
        transfers = list(self.transfers.values())
        if include_terminal:
            return transfers
        return [transfer for transfer in transfers if not transfer.is_terminal]

    def poll_transfer(self, transfer: BridgeTransfer) -> BridgeTransferStatus:
        if transfer.bridge == "wormhole":
            return self._poll_wormhole_transfer(transfer)
        if transfer.bridge == "debridge":
            return self._poll_debridge_transfer(transfer)
        if transfer.bridge == "mayan":
            return self._poll_mayan_transfer(transfer)
        raise ValueError(f"Unsupported bridge: {transfer.bridge}")

    def poll_pending_transfers(self) -> list[BridgeTransferStatus]:
        updates: list[BridgeTransferStatus] = []
        state_changed = False

        for transfer in self.transfers.values():
            if transfer.status in {"failed", "cancelled"}:
                continue
            if transfer.status == "completed" and transfer.completion_alert_sent:
                continue

            before_poll = transfer.to_dict()
            try:
                update = self.poll_transfer(transfer)
            except Exception as exc:
                transfer.last_checked_at = _isoformat(_utcnow())
                transfer.last_error = str(exc)
                logger.warning(
                    "Bridge status lookup failed for %s (%s): %s",
                    transfer.key,
                    transfer.bridge,
                    exc,
                )
                updates.append(
                    BridgeTransferStatus(
                        bridge=transfer.bridge,
                        tracking_id=transfer.tracking_id,
                        status=transfer.status,
                        raw_status=transfer.raw_status,
                        source_tx_hash=transfer.source_tx_hash,
                        destination_tx_hash=transfer.destination_tx_hash,
                        completed_at=transfer.completed_at,
                        error=str(exc),
                    )
                )
                state_changed = True
                continue

            state_changed |= before_poll != transfer.to_dict()
            state_changed |= self._apply_update(transfer, update)
            updates.append(update)

            if transfer.status == "completed" and not transfer.completion_alert_sent:
                self._alert_completion(transfer, update)
                transfer.completion_alert_sent = True
                state_changed = True

        if state_changed:
            self._save_state()
        return updates

    def watch_loop(
        self, *, poll_interval_seconds: float = 30.0, once: bool = False
    ) -> None:
        while True:
            self.poll_pending_transfers()
            if once:
                return
            self.sleep_fn(poll_interval_seconds)

    def _apply_update(
        self, transfer: BridgeTransfer, update: BridgeTransferStatus
    ) -> bool:
        changed = False
        last_checked_at = _isoformat(_utcnow())

        updates = {
            "status": update.status,
            "raw_status": update.raw_status,
            "last_checked_at": last_checked_at,
            "completed_at": update.completed_at,
            "destination_tx_hash": update.destination_tx_hash,
            "last_error": update.error,
        }
        for field_name, value in updates.items():
            if getattr(transfer, field_name) != value:
                setattr(transfer, field_name, value)
                changed = True

        return changed

    def _alert_completion(
        self, transfer: BridgeTransfer, update: BridgeTransferStatus
    ) -> None:
        asset_pair = " -> ".join(
            part
            for part in (transfer.asset_in_symbol, transfer.asset_out_symbol)
            if part
        )
        route = " -> ".join(
            part for part in (transfer.source_chain, transfer.destination_chain) if part
        )
        source_tx = transfer.source_tx_hash or transfer.creation_tx_hash or "n/a"
        destination_tx = update.destination_tx_hash or "n/a"
        completed_at = update.completed_at or _isoformat(_utcnow())

        message_lines = [
            f"Bridge: {transfer.bridge}",
            f"Tracking: {transfer.tracking_id}",
            f"Status: {update.raw_status or update.status}",
            f"Source Tx: {source_tx}",
            f"Destination Tx: {destination_tx}",
            f"Completed At: {completed_at}",
        ]
        if asset_pair:
            message_lines.insert(2, f"Asset: {asset_pair}")
        if route:
            message_lines.insert(2, f"Route: {route}")
        if transfer.destination_address:
            message_lines.append(f"Destination Address: {transfer.destination_address}")

        notifier.warning(
            "\n".join(message_lines),
            title="Bridge Transfer Completed",
        )

        if self.on_completion is not None:
            self.on_completion(transfer, update)

    def _poll_debridge_transfer(self, transfer: BridgeTransfer) -> BridgeTransferStatus:
        order_id = transfer.order_id
        if not order_id:
            creation_tx_hash = _safe_str(transfer.creation_tx_hash)
            if not creation_tx_hash:
                raise ValueError("deBridge transfer is missing order_id and creation_tx_hash")

            url = _build_url(
                DEBRIDGE_STATS_BASE_URL,
                f"Transaction/{urllib.parse.quote(creation_tx_hash, safe='')}/orderIds",
            )
            payload = request_json(
                url,
                timeout=self.poll_timeout,
                headers=DEFAULT_HTTP_HEADERS,
                describe=f"deBridge orderIds {creation_tx_hash}",
            )

            order_ids = payload.get("orderIds", []) if isinstance(payload, dict) else []
            if not order_ids:
                eta, remaining = _estimate_completion(
                    started_at=transfer.created_at,
                    expected_completion_seconds=transfer.expected_completion_seconds,
                )
                return BridgeTransferStatus(
                    bridge="debridge",
                    tracking_id=transfer.tracking_id,
                    status="pending",
                    source_tx_hash=transfer.creation_tx_hash,
                    estimated_completion_time=eta,
                    estimated_seconds_remaining=remaining,
                )

            order_id = _safe_str(order_ids[0])
            if order_id is None:
                raise RuntimeError("deBridge returned a malformed orderIds payload")
            transfer.order_id = order_id

        url = _build_url(
            DEBRIDGE_STATS_BASE_URL,
            f"Orders/{urllib.parse.quote(order_id, safe='')}",
        )
        payload = request_json(
            url,
            timeout=self.poll_timeout,
            headers=DEFAULT_HTTP_HEADERS,
            describe=f"deBridge order {order_id}",
        )
        if not isinstance(payload, dict):
            raise RuntimeError("deBridge returned an invalid order payload")

        raw_status = _safe_str(payload.get("status")) or _safe_str(payload.get("state"))
        normalized = _normalize_debridge_status(raw_status)
        completed_at = _safe_str(payload.get("completedAt"))
        eta, remaining = _estimate_completion(
            started_at=transfer.created_at,
            expected_completion_seconds=transfer.expected_completion_seconds,
        )

        order_id_from_payload = _order_id_string(payload.get("orderId"))
        if order_id_from_payload:
            transfer.order_id = order_id_from_payload

        if transfer.destination_address is None:
            transfer.destination_address = _safe_str(
                _deep_get(payload, "orderStruct", "receiverDst")
            ) or _safe_str(payload.get("receiverDst"))
        if transfer.destination_chain is None:
            transfer.destination_chain = _safe_str(
                _deep_get(payload, "orderStruct", "takeOffer", "chainId")
            ) or _safe_str(_deep_get(payload, "takeOfferWithMetadata", "chainId", "stringValue"))
        if transfer.source_chain is None:
            transfer.source_chain = _safe_str(
                _deep_get(payload, "orderStruct", "giveOffer", "chainId")
            ) or _safe_str(_deep_get(payload, "giveOfferWithMetadata", "chainId", "stringValue"))

        destination_tx_hash = _extract_debridge_destination_tx_hash(payload)
        if normalized == "completed" and completed_at is None:
            completed_at = _isoformat(_utcnow())

        return BridgeTransferStatus(
            bridge="debridge",
            tracking_id=transfer.tracking_id,
            status=normalized,
            raw_status=raw_status,
            source_tx_hash=transfer.source_tx_hash or transfer.creation_tx_hash,
            destination_tx_hash=destination_tx_hash,
            completed_at=completed_at,
            estimated_completion_time=eta,
            estimated_seconds_remaining=remaining,
            details={"order_id": transfer.order_id},
        )

    def _poll_mayan_transfer(self, transfer: BridgeTransfer) -> BridgeTransferStatus:
        source_tx_hash = _safe_str(transfer.source_tx_hash)
        if not source_tx_hash:
            raise ValueError("Mayan transfer is missing source_tx_hash")

        url = _build_url(
            MAYAN_EXPLORER_BASE_URL,
            f"swap/trx/{urllib.parse.quote(source_tx_hash, safe='')}",
        )
        payload = request_json(
            url,
            timeout=self.poll_timeout,
            headers=DEFAULT_HTTP_HEADERS,
            describe=f"Mayan transfer {source_tx_hash}",
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Mayan returned an invalid transfer payload")

        raw_status = _safe_str(payload.get("status"))
        normalized = _normalize_mayan_status(payload)
        completed_at = _safe_str(payload.get("completedAt"))
        eta, remaining = _estimate_completion(
            started_at=payload.get("initiatedAt") or transfer.created_at,
            expected_completion_seconds=transfer.expected_completion_seconds,
        )

        if transfer.source_chain is None:
            transfer.source_chain = _safe_str(payload.get("sourceChain"))
        if transfer.destination_chain is None:
            transfer.destination_chain = _safe_str(payload.get("destChain"))
        if transfer.destination_address is None:
            transfer.destination_address = _safe_str(payload.get("destAddress"))
        if transfer.asset_in_symbol is None:
            transfer.asset_in_symbol = _safe_str(payload.get("fromTokenSymbol"))
        if transfer.asset_out_symbol is None:
            transfer.asset_out_symbol = _safe_str(payload.get("toTokenSymbol"))
        if transfer.amount_in is None:
            transfer.amount_in = _safe_str(payload.get("fromAmount"))
        if transfer.amount_out is None:
            transfer.amount_out = _safe_str(payload.get("toAmount"))
        deadline = _safe_str(payload.get("deadline"))
        if deadline:
            transfer.metadata["deadline"] = deadline

        return BridgeTransferStatus(
            bridge="mayan",
            tracking_id=transfer.tracking_id,
            status=normalized,
            raw_status=raw_status,
            source_tx_hash=source_tx_hash,
            destination_tx_hash=_extract_mayan_destination_tx_hash(payload),
            completed_at=completed_at,
            estimated_completion_time=eta,
            estimated_seconds_remaining=remaining,
            details={"deadline": deadline} if deadline else {},
        )

    def _poll_wormhole_transfer(self, transfer: BridgeTransfer) -> BridgeTransferStatus:
        emitter_address = _safe_str(transfer.emitter_address)
        if not emitter_address:
            raise ValueError("Wormhole transfer is missing emitter_address")

        url = _build_url(
            WORMHOLESCAN_BASE_URL,
            "operations",
            {"address": emitter_address, "pageSize": 25},
        )
        payload = request_json(
            url,
            timeout=self.poll_timeout,
            headers=DEFAULT_HTTP_HEADERS,
            describe=f"Wormholescan operations {emitter_address}",
        )
        operations = payload.get("operations", []) if isinstance(payload, dict) else []

        operation = self._find_wormhole_operation(transfer, operations)
        if operation is None:
            eta, remaining = _estimate_completion(
                started_at=transfer.created_at,
                expected_completion_seconds=transfer.expected_completion_seconds,
            )
            return BridgeTransferStatus(
                bridge="wormhole",
                tracking_id=transfer.tracking_id,
                status="pending",
                source_tx_hash=transfer.source_tx_hash,
                estimated_completion_time=eta,
                estimated_seconds_remaining=remaining,
            )

        raw_status = None
        if operation.get("targetChain"):
            raw_status = _safe_str(_deep_get(operation, "targetChain", "status"))
        if raw_status is None:
            raw_status = _safe_str(_deep_get(operation, "sourceChain", "status"))

        normalized = _normalize_wormhole_status(operation)
        source_tx_hash = _safe_str(
            _deep_get(operation, "sourceChain", "transaction", "txHash")
        )
        completed_at = _safe_str(_deep_get(operation, "targetChain", "timestamp"))
        eta, remaining = _estimate_completion(
            started_at=_deep_get(operation, "sourceChain", "timestamp") or transfer.created_at,
            expected_completion_seconds=transfer.expected_completion_seconds,
        )

        if transfer.operation_id is None:
            transfer.operation_id = _safe_str(operation.get("id"))
        if transfer.sequence is None:
            transfer.sequence = _safe_str(operation.get("sequence"))
        if transfer.source_tx_hash is None:
            transfer.source_tx_hash = source_tx_hash
        if transfer.source_chain is None:
            transfer.source_chain = _safe_str(
                _deep_get(operation, "content", "standarizedProperties", "fromChain")
            )
        if transfer.destination_chain is None:
            transfer.destination_chain = _safe_str(
                _deep_get(operation, "content", "standarizedProperties", "toChain")
            )
        if transfer.destination_address is None:
            transfer.destination_address = _safe_str(
                _deep_get(operation, "content", "standarizedProperties", "toAddress")
            )
        if transfer.amount_out is None:
            transfer.amount_out = _safe_str(
                _deep_get(operation, "content", "standarizedProperties", "amount")
            )

        return BridgeTransferStatus(
            bridge="wormhole",
            tracking_id=transfer.tracking_id,
            status=normalized,
            raw_status=raw_status,
            source_tx_hash=source_tx_hash,
            destination_tx_hash=_safe_str(
                _deep_get(operation, "targetChain", "transaction", "txHash")
            ),
            completed_at=completed_at,
            estimated_completion_time=eta,
            estimated_seconds_remaining=remaining,
            details={
                "operation_id": _safe_str(operation.get("id")),
                "sequence": _safe_str(operation.get("sequence")),
            },
        )

    def _find_wormhole_operation(
        self, transfer: BridgeTransfer, operations: Iterable[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        for operation in operations:
            operation_id = _safe_str(operation.get("id"))
            sequence = _safe_str(operation.get("sequence"))
            source_tx_hash = _safe_str(
                _deep_get(operation, "sourceChain", "transaction", "txHash")
            )

            if transfer.operation_id and operation_id == transfer.operation_id:
                return operation
            if transfer.sequence and sequence == transfer.sequence:
                return operation
            if transfer.source_tx_hash and source_tx_hash == transfer.source_tx_hash:
                return operation

        return None
