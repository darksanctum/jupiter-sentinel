"""
Gas balance management for cross-chain execution wallets.

The manager tracks native gas tokens on:
- Solana (`SOL`)
- Polygon (`POL`)
- Ethereum (`ETH`)

It emits low-balance warnings when a chain falls below its configured warning
threshold and can automatically request small top-up bridges to restore a chain
back to its target gas balance. Bridge execution is intentionally provider
agnostic: callers inject a callback that submits the bridge and returns any
provider-specific tracking metadata.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..config import DATA_DIR
from ..notifications import notifier
from ..resilience import read_json_file, write_json_state

logger = logging.getLogger(__name__)

SOLANA_CHAIN = "solana"
POLYGON_CHAIN = "polygon"
ETHEREUM_CHAIN = "ethereum"

CHAIN_ORDER = (SOLANA_CHAIN, POLYGON_CHAIN, ETHEREUM_CHAIN)
CHAIN_GAS_TOKENS = {
    SOLANA_CHAIN: "SOL",
    POLYGON_CHAIN: "POL",
    ETHEREUM_CHAIN: "ETH",
}
CHAIN_ALIASES = {
    "sol": SOLANA_CHAIN,
    "solana": SOLANA_CHAIN,
    "matic": POLYGON_CHAIN,
    "pol": POLYGON_CHAIN,
    "polygon": POLYGON_CHAIN,
    "eth": ETHEREUM_CHAIN,
    "ethereum": ETHEREUM_CHAIN,
    "mainnet": ETHEREUM_CHAIN,
}

BRIDGE_DIR = DATA_DIR / "bridge"
BRIDGE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_STATE_PATH = BRIDGE_DIR / "gas_manager.json"
DEFAULT_MIN_AUTO_BRIDGE_USD = 3.0
DEFAULT_MAX_AUTO_BRIDGE_USD = 25.0
DEFAULT_BRIDGE_COOLDOWN_SECONDS = 15 * 60
DEFAULT_WARNING_COOLDOWN_SECONDS = 15 * 60
DEFAULT_MAX_HISTORY = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

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


def _normalize_chain(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("chain must be a string")

    normalized = CHAIN_ALIASES.get(value.strip().lower(), value.strip().lower())
    if normalized not in CHAIN_GAS_TOKENS:
        raise ValueError(f"unsupported chain: {value}")
    return normalized


def _coerce_non_negative_float(value: Any, field_name: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc

    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be a finite number >= 0")
    return normalized


def _coerce_positive_float(value: Any, field_name: str) -> float:
    normalized = _coerce_non_negative_float(value, field_name)
    if normalized <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return normalized


def _coerce_optional_positive_float(value: Any, field_name: str) -> Optional[float]:
    if value in (None, ""):
        return None
    return _coerce_positive_float(value, field_name)


def _safe_str(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _new_bridge_id() -> str:
    return f"gas-bridge-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class GasChainConfig:
    chain: str
    token_symbol: str
    minimum_balance: float
    warning_threshold: float
    target_balance: float

    def __post_init__(self) -> None:
        normalized_chain = _normalize_chain(self.chain)
        token_symbol = str(self.token_symbol).strip().upper()
        if not token_symbol:
            raise ValueError("token_symbol cannot be empty")

        minimum_balance = _coerce_non_negative_float(
            self.minimum_balance, "minimum_balance"
        )
        warning_threshold = _coerce_non_negative_float(
            self.warning_threshold, "warning_threshold"
        )
        target_balance = _coerce_positive_float(self.target_balance, "target_balance")

        if target_balance < minimum_balance:
            raise ValueError("target_balance must be >= minimum_balance")

        object.__setattr__(self, "chain", normalized_chain)
        object.__setattr__(self, "token_symbol", token_symbol)
        object.__setattr__(self, "minimum_balance", minimum_balance)
        object.__setattr__(self, "warning_threshold", warning_threshold)
        object.__setattr__(self, "target_balance", target_balance)


DEFAULT_CHAIN_CONFIGS: dict[str, GasChainConfig] = {
    SOLANA_CHAIN: GasChainConfig(
        chain=SOLANA_CHAIN,
        token_symbol="SOL",
        minimum_balance=0.10,
        warning_threshold=0.03,
        target_balance=0.20,
    ),
    POLYGON_CHAIN: GasChainConfig(
        chain=POLYGON_CHAIN,
        token_symbol="POL",
        minimum_balance=3.0,
        warning_threshold=1.0,
        target_balance=6.0,
    ),
    ETHEREUM_CHAIN: GasChainConfig(
        chain=ETHEREUM_CHAIN,
        token_symbol="ETH",
        minimum_balance=0.025,
        warning_threshold=0.010,
        target_balance=0.050,
    ),
}


@dataclass(slots=True)
class GasChainState:
    chain: str
    token_symbol: str
    balance: float = 0.0
    price_usd: Optional[float] = None
    updated_at: Optional[str] = None
    warning_active: bool = False
    last_warning_at: Optional[str] = None
    last_warning_balance: Optional[float] = None
    last_bridge_at: Optional[str] = None
    last_bridge_id: Optional[str] = None
    last_bridge_source_chain: Optional[str] = None
    last_bridge_status: Optional[str] = None
    last_bridge_error: Optional[str] = None

    def __post_init__(self) -> None:
        self.chain = _normalize_chain(self.chain)
        self.token_symbol = str(self.token_symbol).strip().upper()
        if not self.token_symbol:
            raise ValueError("token_symbol cannot be empty")

        self.balance = _coerce_non_negative_float(self.balance, "balance")
        self.price_usd = _coerce_optional_positive_float(self.price_usd, "price_usd")
        self.updated_at = _isoformat(_parse_datetime(self.updated_at) or _utcnow())
        self.last_warning_at = _safe_str(self.last_warning_at)
        self.last_bridge_at = _safe_str(self.last_bridge_at)
        self.last_bridge_id = _safe_str(self.last_bridge_id)
        self.last_bridge_source_chain = (
            None
            if self.last_bridge_source_chain is None
            else _normalize_chain(self.last_bridge_source_chain)
        )
        self.last_bridge_status = _safe_str(self.last_bridge_status)
        self.last_bridge_error = _safe_str(self.last_bridge_error)
        if self.last_warning_balance is not None:
            self.last_warning_balance = _coerce_non_negative_float(
                self.last_warning_balance, "last_warning_balance"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GasChainState":
        return cls(**dict(payload))


@dataclass(frozen=True, slots=True)
class GasChainStatus:
    chain: str
    token_symbol: str
    balance: float
    minimum_balance: float
    warning_threshold: float
    target_balance: float
    price_usd: Optional[float] = None
    updated_at: Optional[str] = None
    last_warning_at: Optional[str] = None
    last_bridge_at: Optional[str] = None

    @property
    def below_warning_threshold(self) -> bool:
        return self.balance < self.warning_threshold

    @property
    def below_minimum_balance(self) -> bool:
        return self.balance < self.minimum_balance

    @property
    def deficit_to_minimum(self) -> float:
        return max(self.minimum_balance - self.balance, 0.0)

    @property
    def deficit_to_target(self) -> float:
        if not self.below_minimum_balance:
            return 0.0
        return max(self.target_balance - self.balance, 0.0)

    @property
    def surplus_above_minimum(self) -> float:
        return max(self.balance - self.minimum_balance, 0.0)

    @property
    def deficit_to_target_usd(self) -> Optional[float]:
        if self.price_usd is None or self.deficit_to_target <= 0:
            return None
        return self.deficit_to_target * self.price_usd

    @property
    def surplus_above_minimum_usd(self) -> Optional[float]:
        if self.price_usd is None or self.surplus_above_minimum <= 0:
            return None
        return self.surplus_above_minimum * self.price_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "token_symbol": self.token_symbol,
            "balance": self.balance,
            "minimum_balance": self.minimum_balance,
            "warning_threshold": self.warning_threshold,
            "target_balance": self.target_balance,
            "price_usd": self.price_usd,
            "updated_at": self.updated_at,
            "last_warning_at": self.last_warning_at,
            "last_bridge_at": self.last_bridge_at,
            "below_warning_threshold": self.below_warning_threshold,
            "below_minimum_balance": self.below_minimum_balance,
            "deficit_to_minimum": self.deficit_to_minimum,
            "deficit_to_target": self.deficit_to_target,
            "surplus_above_minimum": self.surplus_above_minimum,
            "deficit_to_target_usd": self.deficit_to_target_usd,
            "surplus_above_minimum_usd": self.surplus_above_minimum_usd,
        }


@dataclass(frozen=True, slots=True)
class GasBridgeAction:
    bridge_id: str
    source_chain: str
    destination_chain: str
    source_token_symbol: str
    destination_token_symbol: str
    source_amount_estimate: float
    destination_amount: float
    transfer_value_usd: float
    created_at: str
    reason: str = "Maintain minimum gas balance"
    status: str = "planned"
    provider_reference: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_chain", _normalize_chain(self.source_chain))
        object.__setattr__(
            self, "destination_chain", _normalize_chain(self.destination_chain)
        )
        object.__setattr__(
            self,
            "source_token_symbol",
            str(self.source_token_symbol).strip().upper(),
        )
        object.__setattr__(
            self,
            "destination_token_symbol",
            str(self.destination_token_symbol).strip().upper(),
        )
        object.__setattr__(
            self,
            "source_amount_estimate",
            _coerce_non_negative_float(
                self.source_amount_estimate, "source_amount_estimate"
            ),
        )
        object.__setattr__(
            self,
            "destination_amount",
            _coerce_non_negative_float(self.destination_amount, "destination_amount"),
        )
        object.__setattr__(
            self,
            "transfer_value_usd",
            _coerce_non_negative_float(self.transfer_value_usd, "transfer_value_usd"),
        )
        object.__setattr__(
            self, "created_at", _isoformat(_parse_datetime(self.created_at) or _utcnow())
        )
        object.__setattr__(
            self,
            "reason",
            str(self.reason).strip() or "Maintain minimum gas balance",
        )
        object.__setattr__(self, "status", self._normalize_status(self.status))
        object.__setattr__(
            self, "provider_reference", _safe_str(self.provider_reference)
        )
        object.__setattr__(self, "error", _safe_str(self.error))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @staticmethod
    def _normalize_status(value: Any) -> str:
        normalized = str(value or "planned").strip().lower()
        if normalized in {"planned", "submitted", "failed", "skipped"}:
            return normalized
        if normalized in {"pending", "queued", "requested"}:
            return "submitted"
        if normalized in {"error"}:
            return "failed"
        if normalized in {"noop"}:
            return "skipped"
        return "submitted"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GasBridgeAction":
        return cls(**dict(payload))


@dataclass(frozen=True, slots=True)
class GasManagementResult:
    statuses: tuple[GasChainStatus, ...]
    warnings: tuple[str, ...] = ()
    bridge_actions: tuple[GasBridgeAction, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "statuses": [status.as_dict() for status in self.statuses],
            "warnings": list(self.warnings),
            "bridge_actions": [action.to_dict() for action in self.bridge_actions],
        }


BalanceFetcher = Callable[[], Any]
BridgeExecutor = Callable[[GasBridgeAction], Any]


class GasManager:
    """Manage native gas balances across Solana, Polygon, and Ethereum."""

    def __init__(
        self,
        *,
        chain_configs: Optional[Mapping[str, GasChainConfig | Mapping[str, Any]]] = None,
        balance_fetchers: Optional[Mapping[str, BalanceFetcher]] = None,
        bridge_executor: Optional[BridgeExecutor] = None,
        state_path: Path | str = DEFAULT_STATE_PATH,
        min_auto_bridge_usd: float = DEFAULT_MIN_AUTO_BRIDGE_USD,
        max_auto_bridge_usd: float = DEFAULT_MAX_AUTO_BRIDGE_USD,
        bridge_cooldown_seconds: float = DEFAULT_BRIDGE_COOLDOWN_SECONDS,
        warning_cooldown_seconds: float = DEFAULT_WARNING_COOLDOWN_SECONDS,
        max_history: int = DEFAULT_MAX_HISTORY,
        notifier_instance: Any = notifier,
    ) -> None:
        self.chain_configs = self._build_chain_configs(chain_configs)
        self.balance_fetchers = self._normalize_fetchers(balance_fetchers)
        self.bridge_executor = bridge_executor
        self.state_path = Path(state_path).expanduser()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.min_auto_bridge_usd = _coerce_positive_float(
            min_auto_bridge_usd, "min_auto_bridge_usd"
        )
        self.max_auto_bridge_usd = _coerce_positive_float(
            max_auto_bridge_usd, "max_auto_bridge_usd"
        )
        if self.max_auto_bridge_usd < self.min_auto_bridge_usd:
            raise ValueError("max_auto_bridge_usd must be >= min_auto_bridge_usd")

        self.bridge_cooldown_seconds = _coerce_non_negative_float(
            bridge_cooldown_seconds, "bridge_cooldown_seconds"
        )
        self.warning_cooldown_seconds = _coerce_non_negative_float(
            warning_cooldown_seconds, "warning_cooldown_seconds"
        )
        self.max_history = max(int(max_history), 1)
        self.notifier = notifier_instance

        self.states: dict[str, GasChainState] = {
            chain: GasChainState(chain=chain, token_symbol=config.token_symbol)
            for chain, config in self.chain_configs.items()
        }
        self.bridge_actions: list[GasBridgeAction] = []
        self._load_state()

    @staticmethod
    def _build_chain_configs(
        chain_configs: Optional[Mapping[str, GasChainConfig | Mapping[str, Any]]]
    ) -> dict[str, GasChainConfig]:
        configs = dict(DEFAULT_CHAIN_CONFIGS)
        if not chain_configs:
            return configs

        for raw_chain, raw_config in chain_configs.items():
            chain_name = _normalize_chain(raw_chain)
            if isinstance(raw_config, GasChainConfig):
                config = raw_config
            else:
                payload = dict(raw_config)
                payload.setdefault("chain", chain_name)
                payload.setdefault(
                    "token_symbol", CHAIN_GAS_TOKENS.get(chain_name, chain_name.upper())
                )
                config = GasChainConfig(**payload)
            configs[config.chain] = config

        return configs

    @staticmethod
    def _normalize_fetchers(
        balance_fetchers: Optional[Mapping[str, BalanceFetcher]]
    ) -> dict[str, BalanceFetcher]:
        if not balance_fetchers:
            return {}
        return {
            _normalize_chain(chain): fetcher
            for chain, fetcher in balance_fetchers.items()
        }

    def _load_state(self) -> None:
        try:
            payload = read_json_file(self.state_path)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("Failed to read gas manager state %s: %s", self.state_path, exc)
            return

        raw_states = payload.get("chains", {}) if isinstance(payload, dict) else {}
        if isinstance(raw_states, dict):
            for raw_chain, raw_state in raw_states.items():
                try:
                    chain_name = _normalize_chain(raw_chain)
                    state_payload = dict(raw_state)
                    state_payload["chain"] = chain_name
                    state_payload["token_symbol"] = self.chain_configs[chain_name].token_symbol
                    self.states[chain_name] = GasChainState.from_dict(state_payload)
                except Exception as exc:
                    logger.warning(
                        "Skipping invalid gas manager state entry for %s: %s",
                        raw_chain,
                        exc,
                    )

        raw_actions = payload.get("bridge_actions", []) if isinstance(payload, dict) else []
        if isinstance(raw_actions, list):
            loaded_actions: list[GasBridgeAction] = []
            for raw_action in raw_actions:
                try:
                    loaded_actions.append(GasBridgeAction.from_dict(raw_action))
                except Exception as exc:
                    logger.warning("Skipping invalid gas bridge history entry: %s", exc)
            self.bridge_actions = loaded_actions[-self.max_history :]

    def _save_state(self) -> None:
        write_json_state(
            self.state_path,
            {
                "chains": {
                    chain: state.to_dict()
                    for chain, state in self.states.items()
                },
                "bridge_actions": [
                    action.to_dict() for action in self.bridge_actions[-self.max_history :]
                ],
            },
            logger=logger.warning,
        )

    def _build_status(self, chain: str) -> GasChainStatus:
        normalized_chain = _normalize_chain(chain)
        config = self.chain_configs[normalized_chain]
        state = self.states[normalized_chain]
        return GasChainStatus(
            chain=normalized_chain,
            token_symbol=config.token_symbol,
            balance=state.balance,
            minimum_balance=config.minimum_balance,
            warning_threshold=config.warning_threshold,
            target_balance=config.target_balance,
            price_usd=state.price_usd,
            updated_at=state.updated_at,
            last_warning_at=state.last_warning_at,
            last_bridge_at=state.last_bridge_at,
        )

    def get_status(self, chain: str) -> GasChainStatus:
        return self._build_status(chain)

    def list_statuses(self) -> list[GasChainStatus]:
        ordered = [
            chain for chain in CHAIN_ORDER if chain in self.chain_configs
        ] + [
            chain
            for chain in self.chain_configs
            if chain not in CHAIN_ORDER
        ]
        return [self._build_status(chain) for chain in ordered]

    def list_bridge_actions(self, *, limit: Optional[int] = None) -> list[GasBridgeAction]:
        actions = list(self.bridge_actions)
        if limit is None or limit >= len(actions):
            return actions
        return actions[-limit:]

    def _update_balance_state(
        self,
        chain: str,
        balance: Any,
        *,
        price_usd: Any = None,
        observed_at: Any = None,
    ) -> GasChainState:
        normalized_chain = _normalize_chain(chain)
        state = self.states[normalized_chain]
        config = self.chain_configs[normalized_chain]

        state.balance = _coerce_non_negative_float(balance, "balance")
        if price_usd is not None:
            state.price_usd = _coerce_positive_float(price_usd, "price_usd")
        state.updated_at = _isoformat(_parse_datetime(observed_at) or _utcnow())
        state.token_symbol = config.token_symbol

        if state.balance >= config.warning_threshold and state.warning_active:
            state.warning_active = False

        return state

    def update_balance(
        self,
        chain: str,
        balance: Any,
        *,
        price_usd: Any = None,
        observed_at: Any = None,
    ) -> GasChainState:
        state = self._update_balance_state(
            chain,
            balance,
            price_usd=price_usd,
            observed_at=observed_at,
        )
        self._save_state()
        return state

    def update_balances(
        self,
        balances: Mapping[str, Any],
        *,
        prices_usd: Optional[Mapping[str, Any]] = None,
        observed_at: Any = None,
    ) -> dict[str, GasChainState]:
        updated: dict[str, GasChainState] = {}
        for chain, raw_value in balances.items():
            if isinstance(raw_value, Mapping):
                balance = raw_value.get("balance")
                price_usd = raw_value.get("price_usd")
            else:
                balance = raw_value
                price_usd = None
                if prices_usd is not None:
                    price_usd = prices_usd.get(chain)

            updated[_normalize_chain(chain)] = self._update_balance_state(
                chain,
                balance,
                price_usd=price_usd,
                observed_at=observed_at,
            )

        if updated:
            self._save_state()
        return updated

    def refresh_balances(self) -> dict[str, GasChainState]:
        updated: dict[str, GasChainState] = {}
        for chain, fetcher in self.balance_fetchers.items():
            try:
                payload = fetcher()
            except Exception as exc:
                logger.warning("Gas balance refresh failed for %s: %s", chain, exc)
                continue

            if isinstance(payload, Mapping):
                balance = payload.get("balance")
                price_usd = payload.get("price_usd")
                observed_at = payload.get("observed_at")
            else:
                balance = payload
                price_usd = None
                observed_at = None

            updated[chain] = self._update_balance_state(
                chain,
                balance,
                price_usd=price_usd,
                observed_at=observed_at,
            )

        if updated:
            self._save_state()
        return updated

    def run_cycle(
        self,
        *,
        refresh_balances: bool = False,
        auto_bridge: bool = True,
    ) -> GasManagementResult:
        if refresh_balances:
            self.refresh_balances()

        statuses = tuple(self.list_statuses())
        warnings = tuple(self._emit_low_balance_warnings(statuses))
        bridge_actions = (
            tuple(self._auto_bridge(statuses))
            if auto_bridge
            else ()
        )
        self._save_state()
        return GasManagementResult(
            statuses=tuple(self.list_statuses()),
            warnings=warnings,
            bridge_actions=bridge_actions,
        )

    def _emit_low_balance_warnings(
        self, statuses: tuple[GasChainStatus, ...]
    ) -> list[str]:
        messages: list[str] = []
        now = _utcnow()

        for status in statuses:
            state = self.states[status.chain]
            if not status.below_warning_threshold:
                if state.warning_active:
                    state.warning_active = False
                continue

            should_notify = not state.warning_active
            if not should_notify:
                last_warning_at = _parse_datetime(state.last_warning_at)
                if last_warning_at is None:
                    should_notify = True
                else:
                    should_notify = now - last_warning_at >= timedelta(
                        seconds=self.warning_cooldown_seconds
                    )

            if not should_notify:
                continue

            message = (
                f"{status.chain.title()} gas is low: {status.balance:.6f} "
                f"{status.token_symbol} available "
                f"(warning threshold {status.warning_threshold:.6f}, "
                f"minimum {status.minimum_balance:.6f})."
            )
            self.notifier.warning(message, title="Low Gas Balance")
            state.warning_active = True
            state.last_warning_at = _isoformat(now)
            state.last_warning_balance = status.balance
            messages.append(message)

        return messages

    def _auto_bridge(self, statuses: tuple[GasChainStatus, ...]) -> list[GasBridgeAction]:
        working_balances = {status.chain: status.balance for status in statuses}
        working_prices = {status.chain: status.price_usd for status in statuses}
        actions: list[GasBridgeAction] = []
        now = _utcnow()
        now_iso = _isoformat(now)

        for destination in statuses:
            if not destination.below_minimum_balance:
                continue
            if self._bridge_cooldown_active(destination.chain, now):
                continue

            destination_price = working_prices.get(destination.chain)
            if destination_price is None or destination_price <= 0:
                logger.warning(
                    "Cannot auto-bridge %s gas: missing %s price_usd",
                    destination.chain,
                    destination.token_symbol,
                )
                continue

            destination_target = self.chain_configs[destination.chain].target_balance
            destination_deficit = max(
                destination_target - working_balances[destination.chain], 0.0
            )
            if destination_deficit <= 0:
                continue

            remaining_usd = min(
                destination_deficit * destination_price,
                self.max_auto_bridge_usd,
            )
            if remaining_usd < self.min_auto_bridge_usd:
                continue

            source_candidates = sorted(
                (
                    status
                    for status in statuses
                    if status.chain != destination.chain
                ),
                key=lambda item: item.surplus_above_minimum_usd or 0.0,
                reverse=True,
            )

            for source in source_candidates:
                source_price = working_prices.get(source.chain)
                if source_price is None or source_price <= 0:
                    continue

                available_source_native = max(
                    working_balances[source.chain]
                    - self.chain_configs[source.chain].minimum_balance,
                    0.0,
                )
                available_source_usd = available_source_native * source_price

                if available_source_usd < self.min_auto_bridge_usd:
                    continue

                transfer_usd = min(remaining_usd, available_source_usd)
                if transfer_usd < self.min_auto_bridge_usd:
                    continue

                action = GasBridgeAction(
                    bridge_id=_new_bridge_id(),
                    source_chain=source.chain,
                    destination_chain=destination.chain,
                    source_token_symbol=self.chain_configs[source.chain].token_symbol,
                    destination_token_symbol=self.chain_configs[
                        destination.chain
                    ].token_symbol,
                    source_amount_estimate=transfer_usd / source_price,
                    destination_amount=transfer_usd / destination_price,
                    transfer_value_usd=transfer_usd,
                    created_at=now_iso,
                    status="planned",
                )
                action = self._submit_bridge(action)
                self._record_bridge_action(action)
                actions.append(action)

                working_balances[source.chain] -= action.source_amount_estimate
                working_balances[destination.chain] += action.destination_amount
                remaining_usd -= action.transfer_value_usd

                if remaining_usd < self.min_auto_bridge_usd:
                    break

        return actions

    def _bridge_cooldown_active(self, chain: str, now: datetime) -> bool:
        state = self.states[_normalize_chain(chain)]
        last_bridge_at = _parse_datetime(state.last_bridge_at)
        if last_bridge_at is None:
            return False
        return now - last_bridge_at < timedelta(seconds=self.bridge_cooldown_seconds)

    def _submit_bridge(self, action: GasBridgeAction) -> GasBridgeAction:
        if self.bridge_executor is None:
            return action

        try:
            result = self.bridge_executor(action)
        except Exception as exc:
            return replace(
                action,
                status="failed",
                error=str(exc),
            )

        if result is None:
            return replace(action, status="submitted")

        if isinstance(result, str):
            return replace(
                action,
                status="submitted",
                provider_reference=result,
            )

        if isinstance(result, Mapping):
            status = GasBridgeAction._normalize_status(result.get("status"))
            provider_reference = (
                _safe_str(result.get("provider_reference"))
                or _safe_str(result.get("tracking_id"))
                or _safe_str(result.get("reference"))
                or _safe_str(result.get("id"))
            )
            metadata = dict(result)
            error = _safe_str(result.get("error"))
            return replace(
                action,
                status=status,
                provider_reference=provider_reference,
                error=error,
                metadata=metadata,
            )

        return replace(
            action,
            status="submitted",
            metadata={"result": result},
        )

    def _record_bridge_action(self, action: GasBridgeAction) -> None:
        destination_state = self.states[action.destination_chain]
        destination_state.last_bridge_at = action.created_at
        destination_state.last_bridge_id = action.bridge_id
        destination_state.last_bridge_source_chain = action.source_chain
        destination_state.last_bridge_status = action.status
        destination_state.last_bridge_error = action.error

        self.bridge_actions.append(action)
        if len(self.bridge_actions) > self.max_history:
            self.bridge_actions = self.bridge_actions[-self.max_history :]


__all__ = [
    "CHAIN_GAS_TOKENS",
    "DEFAULT_CHAIN_CONFIGS",
    "DEFAULT_STATE_PATH",
    "ETHEREUM_CHAIN",
    "GasBridgeAction",
    "GasChainConfig",
    "GasChainState",
    "GasChainStatus",
    "GasManagementResult",
    "GasManager",
    "POLYGON_CHAIN",
    "SOLANA_CHAIN",
]
