"""Session-aware London sweep strategy primitives.

The strategy is intentionally deterministic.  It consumes confirmed bars and
sweeps produced by :mod:`jevloop.market_structure`, while Jev is only used as
an optional entry filter at the policy boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from .market_structure import Bar, Sweep
from .strategy import THRESHOLDS


@dataclass(frozen=True)
class SessionConfig:
    """UTC session and trigger settings.

    The defaults model the London morning window used by the reference rules.
    ``end_hour_utc`` may be lower than ``start_hour_utc`` for an overnight
    session, although the default and the execution model use a same-day
    window.
    """

    start_hour_utc: int = 8
    start_minute_utc: int = 0
    end_hour_utc: int = 11
    end_minute_utc: int = 0
    trigger_expiry_s: float = 60.0

    def __post_init__(self) -> None:
        if not 0 <= self.start_hour_utc < 24 or not 0 <= self.end_hour_utc < 24:
            raise ValueError("session hours must be in [0, 23]")
        if not 0 <= self.start_minute_utc < 60 or not 0 <= self.end_minute_utc < 60:
            raise ValueError("session minutes must be in [0, 59]")
        if self.trigger_expiry_s <= 0:
            raise ValueError("trigger_expiry_s must be positive")


def session_bounds(ts: float, config: SessionConfig = SessionConfig()) -> tuple[float, float]:
    """Return the UTC start and end timestamps for the session containing ``ts``."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    start = dt.replace(
        hour=config.start_hour_utc,
        minute=config.start_minute_utc,
        second=0,
        microsecond=0,
    )
    end = dt.replace(
        hour=config.end_hour_utc,
        minute=config.end_minute_utc,
        second=0,
        microsecond=0,
    )
    overnight = end <= start
    if overnight:
        end += timedelta(days=1)
    if overnight and dt < start:
        # A timestamp in the early morning belongs to the previous overnight
        # session when the configured window crosses midnight.
        previous = start - timedelta(days=1)
        start = previous
        end = end - timedelta(days=1)
    return start.timestamp(), end.timestamp()


@dataclass(frozen=True)
class SetupCandidate:
    setup_id: int
    direction: str  # "long" or "short"
    sweep: Sweep
    refinement: Bar
    trigger_price: float
    expires_at: float


@dataclass(frozen=True)
class EntrySignal:
    setup_id: int
    direction: str
    timestamp: float
    entry_price: float
    stop_price: float
    sweep_extreme: float


@dataclass(frozen=True)
class SessionSnapshot:
    session_start: float | None
    session_end: float | None
    bias: str | None
    bias_sweep: Sweep | None
    pending_setup: SetupCandidate | None
    invalidated: bool


class LondonSweepEngine:
    """Drive the 15m bias -> 1m refinement -> 5s trigger state machine."""

    def __init__(self, config: SessionConfig | None = None):
        self.config = config or SessionConfig()
        self._session_start: float | None = None
        self._session_end: float | None = None
        self._bias: str | None = None
        self._bias_sweep: Sweep | None = None
        self._pending_setup: SetupCandidate | None = None
        self._pending_sweep: Sweep | None = None
        self._invalidated = False
        self._next_setup_id = 1

    @property
    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            self._session_start,
            self._session_end,
            self._bias,
            self._bias_sweep,
            self._pending_setup,
            self._invalidated,
        )

    def _roll_session(self, ts: float) -> tuple[float, float]:
        start, end = session_bounds(ts, self.config)
        if start != self._session_start:
            self._session_start, self._session_end = start, end
            self._bias = None
            self._bias_sweep = None
            self._pending_setup = None
            self._pending_sweep = None
            self._invalidated = False
        return start, end

    def _in_session(self, ts: float) -> bool:
        return self._session_start is not None and self._session_end is not None and self._session_start <= ts < self._session_end

    def on_15m_sweep(self, sweep: Sweep) -> None:
        """Apply a confirmed 15m sweep to the current session bias."""
        ts = sweep.timestamp
        start, end = self._roll_session(ts)
        if ts < start:
            # Last confirmed sweep before the session establishes direction.
            self._bias, self._bias_sweep = sweep.direction, sweep
            return
        if ts >= end:
            return
        if self._bias is None:
            # A run started after the session opened can still recover the
            # bias from the first confirmed sweep observed in-session.
            self._bias, self._bias_sweep = sweep.direction, sweep
            return
        if sweep.direction != self._bias:
            self._invalidated = True
            self._pending_setup = None
            self._pending_sweep = None
        else:
            self._bias_sweep = sweep

    def on_1m_sweep(self, sweep: Sweep, position_open: bool = False) -> SetupCandidate | None:
        """Start a setup when a 1m sweep agrees with the active bias."""
        ts = sweep.timestamp
        start, end = self._roll_session(ts)
        if not self._in_session(ts) or ts >= end or self._bias is None:
            return None
        if self._invalidated or position_open or sweep.direction != self._bias:
            return None
        # Do not replace a still-live setup with a later sweep.
        if self._pending_setup is not None:
            return self._pending_setup
        self._pending_sweep = sweep
        # The refinement candle is supplied by on_1m_bar after this sweep.
        return None

    def on_1m_bar(self, bar: Bar, sweep: Sweep | None = None, position_open: bool = False) -> SetupCandidate | None:
        """Create a refinement setup from the first bar after a 1m sweep.

        Pass the sweep that closed on the immediately preceding 1m bar.  This
        explicit argument avoids look-ahead and makes replay tests deterministic.
        """
        ts = bar.start_ts
        start, end = self._roll_session(ts)
        if not self._in_session(ts) or ts >= end or position_open or self._invalidated:
            return None
        sweep = sweep or self._pending_sweep
        if sweep is None or sweep.direction != self._bias or sweep.timestamp > ts:
            return self._pending_setup
        if self._pending_setup is not None:
            return self._pending_setup
        trigger = bar.high if sweep.direction == "long" else bar.low
        self._pending_setup = SetupCandidate(
            self._next_setup_id,
            sweep.direction,
            sweep,
            bar,
            trigger,
            min(bar.end_ts + self.config.trigger_expiry_s, end),
        )
        self._pending_sweep = None
        self._next_setup_id += 1
        return self._pending_setup

    def on_5s_bar(self, bar: Bar, position_open: bool = False) -> EntrySignal | None:
        """Return one entry signal when a 5s bar breaks the refinement."""
        setup = self._pending_setup
        if setup is None:
            return None
        if position_open or self._invalidated or not self._in_session(bar.start_ts):
            return None
        if bar.start_ts >= setup.expires_at:
            self._pending_setup = None
            return None
        broke = (
            bar.high > setup.trigger_price
            if setup.direction == "long"
            else bar.low < setup.trigger_price
        )
        if not broke:
            return None
        entry = setup.trigger_price
        stop = setup.sweep.extreme
        self._pending_setup = None
        return EntrySignal(setup.setup_id, setup.direction, bar.end_ts, entry, stop, stop)

    def close_session(self, ts: float) -> None:
        """Expire pending triggers at session close without changing history."""
        if self._session_end is not None and ts >= self._session_end:
            self._pending_setup = None
            self._pending_sweep = None


def jev_allows_entry(
    answers: Mapping[str, Mapping[str, object]],
    direction: str,
    *,
    stale: bool = False,
    late: bool = False,
) -> tuple[bool, str]:
    """Apply Jev only as a typed filter; rules own all levels and sizing."""
    if stale:
        return False, "stale market data"
    if late:
        return False, "decision arrived after tick budget"
    regime = answers.get("regime", {})
    if str(regime.get("choice", regime.get("label", ""))).lower() in {"crisis", "panic"}:
        return False, "Jev regime veto"
    execution = answers.get("execution_health", {})
    if float(execution.get("score", execution.get("noul", 0.0))) < 2.0:
        return False, "execution health below floor"
    environment = answers.get("quote_environment", {})
    if float(environment.get("score", environment.get("noul", 0.0))) < 1.0:
        return False, "quote environment below floor"
    answer_direction = answers.get("direction", {})
    choice = str(answer_direction.get("choice", "neutral")).lower()
    expected = "up" if direction == "long" else "down"
    confidence = float(answer_direction.get("confidence", 0.0))
    if choice != expected or confidence <= THRESHOLDS.direction_confidence_threshold:
        return False, "Jev direction filter"
    return True, "Jev filter passed"
