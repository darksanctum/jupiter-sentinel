"""
Jupiter Sentinel - Correlation Tracker
Tracks watchlist token correlations and blocks concentrated entries.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .config import DATA_DIR, PRICE_HISTORY_LEN, SCAN_PAIRS, SOL_MINT, USDC_MINT
from .resilience import read_json_file, write_json_state

HIGH_CORRELATION_THRESHOLD = 0.8
CORRELATION_REFRESH_INTERVAL_SECONDS = 3600.0


def _utcnow() -> str:
    """Function docstring."""
    return datetime.utcnow().isoformat()


def _as_float(value: Any, default: float = 0.0) -> float:
    """Function docstring."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _read_field(payload: Any, key: str, default: Any = None) -> Any:
    """Function docstring."""
    if isinstance(payload, Mapping):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _parse_timestamp(value: Any) -> Optional[float]:
    """Function docstring."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _clip_correlation(value: Any) -> float:
    """Function docstring."""
    return max(min(_as_float(value, 0.0), 1.0), -1.0)


def _symbol_from_pair(
    pair_name: str,
    input_mint: str,
    output_mint: str,
    tracked_mint: str,
) -> str:
    """Function docstring."""
    base, _, quote = pair_name.partition("/")
    base = base.strip()
    quote = quote.strip()

    if tracked_mint == input_mint and base:
        return base
    if tracked_mint == output_mint and quote:
        return quote
    return base or quote or tracked_mint[:6]


def _derive_tracked_mint(input_mint: str, output_mint: str) -> Optional[str]:
    """Function docstring."""
    if input_mint not in {SOL_MINT, USDC_MINT}:
        return input_mint
    if output_mint not in {SOL_MINT, USDC_MINT}:
        return output_mint
    return None


def _pair_priority(input_mint: str, output_mint: str) -> int:
    """Prefer USD-quoted watchlist feeds when multiple pairs map to one token."""
    if input_mint == USDC_MINT or output_mint == USDC_MINT:
        return 0
    if input_mint == SOL_MINT or output_mint == SOL_MINT:
        return 1
    return 2


def _prices_from_feed(feed: Any, *, lookback_points: int) -> list[float]:
    """Function docstring."""
    history = _read_field(feed, "history", [])
    prices = [_as_float(_read_field(point, "price"), float("nan")) for point in history]
    clean = [price for price in prices if math.isfinite(price) and price > 0]
    if lookback_points > 0:
        return clean[-lookback_points:]
    return clean


def _returns_from_prices(prices: Sequence[float]) -> list[float]:
    """Function docstring."""
    returns: list[float] = []
    for index in range(1, len(prices)):
        previous = prices[index - 1]
        current = prices[index]
        if previous <= 0:
            continue
        returns.append((current - previous) / previous)
    return returns


def _pearson_correlation(left: Sequence[float], right: Sequence[float]) -> float:
    """Function docstring."""
    sample_size = min(len(left), len(right))
    if sample_size < 2:
        return 0.0

    left_values = list(left[-sample_size:])
    right_values = list(right[-sample_size:])
    left_mean = math.fsum(left_values) / sample_size
    right_mean = math.fsum(right_values) / sample_size

    covariance = 0.0
    left_variance = 0.0
    right_variance = 0.0
    for left_value, right_value in zip(left_values, right_values):
        left_delta = left_value - left_mean
        right_delta = right_value - right_mean
        covariance += left_delta * right_delta
        left_variance += left_delta * left_delta
        right_variance += right_delta * right_delta

    if left_variance <= 0 or right_variance <= 0:
        return 0.0
    return covariance / math.sqrt(left_variance * right_variance)


@dataclass(frozen=True)
class TrackedToken:
    """Function docstring."""

    mint: str
    symbol: str
    pair: str
    input_mint: str
    output_mint: str


class CorrelationTracker:
    """Maintain a cached token correlation matrix for the active watchlist."""

    def __init__(
        self,
        path: Path | str = DATA_DIR / "correlations.json",
        *,
        refresh_interval_seconds: float = CORRELATION_REFRESH_INTERVAL_SECONDS,
        threshold: float = HIGH_CORRELATION_THRESHOLD,
        lookback_points: int = PRICE_HISTORY_LEN,
        watchlist: Optional[Sequence[tuple[str, str, str]]] = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        """Function docstring."""
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        if lookback_points < 3:
            raise ValueError("lookback_points must be at least 3")

        self.path = Path(path).expanduser()
        self.refresh_interval_seconds = float(refresh_interval_seconds)
        self.threshold = float(threshold)
        self.lookback_points = int(lookback_points)
        self.time_fn = time_fn
        self.tokens = self._build_watchlist_tokens(watchlist or SCAN_PAIRS)
        self.matrix = self._empty_matrix()
        self.updated_at: Optional[str] = None
        self.updated_at_ts: Optional[float] = None
        self._load()

    def _build_watchlist_tokens(
        self,
        watchlist: Sequence[tuple[str, str, str]],
    ) -> dict[str, TrackedToken]:
        """Function docstring."""
        tracked: dict[str, TrackedToken] = {}

        for input_mint, output_mint, pair_name in watchlist:
            mint = _derive_tracked_mint(input_mint, output_mint)
            if not mint:
                continue

            token = TrackedToken(
                mint=mint,
                symbol=_symbol_from_pair(pair_name, input_mint, output_mint, mint),
                pair=pair_name,
                input_mint=input_mint,
                output_mint=output_mint,
            )
            existing = tracked.get(mint)
            if existing is None or _pair_priority(
                input_mint, output_mint
            ) < _pair_priority(existing.input_mint, existing.output_mint):
                tracked[mint] = token

        return tracked

    def _empty_matrix(self) -> dict[str, dict[str, float]]:
        """Function docstring."""
        return {
            left: {
                right: 1.0 if left == right else 0.0 for right in self.tokens.keys()
            }
            for left in self.tokens.keys()
        }

    def _normalize_matrix(self, raw: Any) -> dict[str, dict[str, float]]:
        """Function docstring."""
        matrix = self._empty_matrix()
        if not isinstance(raw, Mapping):
            return matrix

        for left in self.tokens.keys():
            row = raw.get(left, {})
            mirror_row = raw if isinstance(raw, Mapping) else {}
            for right in self.tokens.keys():
                if left == right:
                    matrix[left][right] = 1.0
                    continue

                value = None
                if isinstance(row, Mapping) and right in row:
                    value = row[right]
                else:
                    reverse_row = mirror_row.get(right, {})
                    if isinstance(reverse_row, Mapping) and left in reverse_row:
                        value = reverse_row[left]

                matrix[left][right] = _clip_correlation(value)

        return matrix

    def _load(self) -> None:
        """Function docstring."""
        try:
            payload = read_json_file(self.path)
        except FileNotFoundError:
            return
        except Exception as exc:
            logging.warning("Could not read correlation cache %s: %s", self.path, exc)
            return

        if not isinstance(payload, Mapping):
            return

        self.matrix = self._normalize_matrix(payload.get("matrix", {}))
        updated_at = payload.get("updated_at")
        parsed_timestamp = _parse_timestamp(updated_at)
        self.updated_at = updated_at if isinstance(updated_at, str) else None
        self.updated_at_ts = parsed_timestamp

    def _payload(self) -> dict[str, Any]:
        """Function docstring."""
        return {
            "updated_at": self.updated_at or _utcnow(),
            "refresh_interval_seconds": self.refresh_interval_seconds,
            "threshold": self.threshold,
            "lookback_points": self.lookback_points,
            "tokens": {
                mint: {
                    "symbol": token.symbol,
                    "pair": token.pair,
                    "input_mint": token.input_mint,
                    "output_mint": token.output_mint,
                }
                for mint, token in self.tokens.items()
            },
            "matrix": self.matrix,
        }

    def _persist(self) -> None:
        """Function docstring."""
        write_json_state(self.path, self._payload())

    def _due_for_refresh(self) -> bool:
        """Function docstring."""
        if self.updated_at is None or self.updated_at_ts is None:
            return True
        return (self.time_fn() - self.updated_at_ts) >= self.refresh_interval_seconds

    def refresh_if_due(
        self,
        feeds_by_pair: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        """Recalculate the matrix when the hourly refresh window has elapsed."""
        if not force and not self._due_for_refresh():
            return False

        existing = self._normalize_matrix(self.matrix)
        returns_by_mint = {
            mint: _returns_from_prices(
                _prices_from_feed(
                    feeds_by_pair.get(token.pair),
                    lookback_points=self.lookback_points,
                )
            )
            for mint, token in self.tokens.items()
        }

        next_matrix = self._empty_matrix()
        mints = list(self.tokens.keys())
        for left_index, left_mint in enumerate(mints):
            for right_mint in mints[left_index + 1 :]:
                left_returns = returns_by_mint.get(left_mint, [])
                right_returns = returns_by_mint.get(right_mint, [])

                if min(len(left_returns), len(right_returns)) >= 2:
                    corr = _clip_correlation(
                        _pearson_correlation(left_returns, right_returns)
                    )
                else:
                    corr = existing.get(left_mint, {}).get(
                        right_mint,
                        existing.get(right_mint, {}).get(left_mint, 0.0),
                    )

                next_matrix[left_mint][right_mint] = corr
                next_matrix[right_mint][left_mint] = corr

        self.matrix = next_matrix
        self.updated_at_ts = float(self.time_fn())
        self.updated_at = _utcnow()
        self._persist()
        return True

    def correlation(self, left_mint: str, right_mint: str) -> float:
        """Function docstring."""
        if left_mint == right_mint and left_mint:
            return 1.0

        return _clip_correlation(
            self.matrix.get(left_mint, {}).get(
                right_mint,
                self.matrix.get(right_mint, {}).get(left_mint, 0.0),
            )
        )

    def _position_token_mint(
        self,
        position: Any,
        *,
        pair_lookup: Optional[Mapping[str, tuple[str, str]]] = None,
        position_meta: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Optional[str]:
        """Function docstring."""
        input_mint = _read_field(position, "input_mint")
        output_mint = _read_field(position, "output_mint")
        pair = str(_read_field(position, "pair", ""))

        if isinstance(input_mint, str) and isinstance(output_mint, str):
            tracked = _derive_tracked_mint(input_mint, output_mint)
            if tracked:
                return tracked

        meta = (position_meta or {}).get(pair, {})
        meta_input = meta.get("scan_input_mint")
        meta_output = meta.get("scan_output_mint")
        if isinstance(meta_input, str) and isinstance(meta_output, str):
            tracked = _derive_tracked_mint(meta_input, meta_output)
            if tracked:
                return tracked

        if pair_lookup and pair in pair_lookup:
            lookup_input, lookup_output = pair_lookup[pair]
            return _derive_tracked_mint(lookup_input, lookup_output)

        return None

    def find_correlated_open_position(
        self,
        candidate_pair: str,
        candidate_input_mint: str,
        candidate_output_mint: str,
        open_positions: Sequence[Any],
        *,
        pair_lookup: Optional[Mapping[str, tuple[str, str]]] = None,
        position_meta: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Optional[dict[str, Any]]:
        """Return the strongest open-position conflict above the threshold."""
        candidate_mint = _derive_tracked_mint(candidate_input_mint, candidate_output_mint)
        if not candidate_mint:
            return None

        candidate_token = self.tokens.get(candidate_mint)
        best_conflict: Optional[dict[str, Any]] = None

        for position in open_positions:
            if str(_read_field(position, "status", "open")) != "open":
                continue

            open_pair = str(_read_field(position, "pair", ""))
            open_mint = self._position_token_mint(
                position,
                pair_lookup=pair_lookup,
                position_meta=position_meta,
            )
            if not open_mint:
                continue

            corr = self.correlation(candidate_mint, open_mint)
            if corr <= self.threshold:
                continue

            open_token = self.tokens.get(open_mint)
            conflict = {
                "candidate_pair": candidate_pair,
                "candidate_mint": candidate_mint,
                "candidate_symbol": (
                    candidate_token.symbol
                    if candidate_token
                    else _symbol_from_pair(
                        candidate_pair,
                        candidate_input_mint,
                        candidate_output_mint,
                        candidate_mint,
                    )
                ),
                "open_pair": open_pair,
                "open_mint": open_mint,
                "open_symbol": (
                    open_token.symbol
                    if open_token
                    else open_pair.partition("/")[0] or open_mint[:6]
                ),
                "correlation": corr,
                "threshold": self.threshold,
            }
            if best_conflict is None or corr > best_conflict["correlation"]:
                best_conflict = conflict

        return best_conflict
