"""The deterministic ISX Intent -> S1 -> AOI -> S2 -> X state machine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from .models import (
    Candle,
    Direction,
    ISXDecision,
    ISXPhase,
    ISXState,
    Transition,
)
from .structure import (
    completed_candles,
    detect_breaks,
    detect_pivots,
    latest_intent_break,
    range_anchors,
    structure_direction,
)


class ISXEngine:
    """Pure stateful evaluator fed only completed H4/H1/M15 candles.

    The engine does not know Alpaca, Jev, order sizing, or risk. Replaying the
    same candle sets into a fresh engine produces the same setup id and state;
    replaying them on the same engine is idempotent.
    """

    def __init__(self, pivot_left: int = 2, pivot_right: int = 2):
        self.pivot_left = pivot_left
        self.pivot_right = pivot_right
        self.state = ISXState()
        self._fingerprint: tuple | None = None

    def update(self, bars: Mapping[str, Sequence[Candle]]) -> ISXDecision:
        h4 = completed_candles(bars.get("H4", ()))
        h1 = completed_candles(bars.get("H1", ()))
        m15 = completed_candles(bars.get("M15", ()))
        fingerprint = tuple(
            (name, tuple(c.timestamp for c in series))
            for name, series in (("H4", h4), ("H1", h1), ("M15", m15))
        )
        if fingerprint == self._fingerprint:
            return self._decision("duplicate closed-candle set")
        self._fingerprint = fingerprint

        if not h4 or not h1 or not m15:
            self._stand_down("missing completed H4, H1, or M15 candles")
            return self._decision(self.state.stand_down_reason or "missing bars")

        h4_direction = structure_direction(h4, self.pivot_left, self.pivot_right)
        h1_direction = structure_direction(h1, self.pivot_left, self.pivot_right)
        self.state.h4_direction = h4_direction
        self.state.h1_direction = h1_direction
        self.state.last_closed_candle = max(h4[-1].timestamp, h1[-1].timestamp, m15[-1].timestamp)

        if h4_direction is Direction.NEUTRAL or h1_direction is Direction.NEUTRAL:
            self._stand_down("H4 or H1 is structurally neutral")
            return self._decision(self.state.stand_down_reason or "neutral structure")
        if h4_direction is not h1_direction:
            self._stand_down("H4/H1 structure disagreement")
            return self._decision(self.state.stand_down_reason or "timeframe disagreement")

        intent = h4_direction
        intent_break = latest_intent_break(h1, intent, self.pivot_left, self.pivot_right)
        anchors = range_anchors(h1, intent_break, intent, self.pivot_left, self.pivot_right) if intent_break else None
        if intent_break is None or anchors is None:
            self._stand_down("aligned structure has no confirmed H1 intent break")
            return self._decision(self.state.stand_down_reason or "no intent break")

        setup_id = self._setup_id(intent, intent_break.timestamp)
        if self.state.setup_id != setup_id:
            self.state = ISXState(
                setup_id=setup_id,
                phase=ISXPhase.WAIT_S1,
                h4_direction=h4_direction,
                h1_direction=h1_direction,
                intent=intent,
                ex=anchors.ex,
                px=anchors.px,
                ep=anchors.ep,
                invalidation_price=anchors.ex,
                last_closed_candle=self.state.last_closed_candle,
            )
            self._transition(intent_break.timestamp, ISXPhase.WAIT_INTENT, ISXPhase.WAIT_S1, "INTENT")

        if self.state.phase is ISXPhase.INVALIDATED:
            self.state.stand_down_reason = "setup invalidated; waiting for a new H1 Intent"
            return self._decision(self.state.stand_down_reason)

        if self.state.phase in (ISXPhase.WAIT_S1, ISXPhase.REASSESS):
            s1 = self._first_shift_after(m15, self.state.s1_time or intent_break.timestamp, intent, opposite=True)
            if s1 is None:
                self.state.stand_down_reason = "waiting for M15 S1 against Intent"
                return self._decision(self.state.stand_down_reason)
            self.state.s1_time = s1.timestamp
            self.state.phase = ISXPhase.WAIT_AOI
            self.state.x_ready = False
            self.state.aoi_qualified = False
            self.state.s2_time = None
            self._transition(s1.timestamp, ISXPhase.WAIT_S1, ISXPhase.WAIT_AOI, "S1")

        latest = m15[-1]
        self.state.fib_retracement = self._fib(latest.close)
        if not self.state.aoi_qualified:
            for bar in m15:
                if self.state.s1_time is None or bar.timestamp <= self.state.s1_time:
                    continue
                if self._invalidated(bar.close):
                    self.state.phase = ISXPhase.INVALIDATED
                    self.state.x_ready = False
                    self.state.stand_down_reason = "close violated EX/1.0 invalidation boundary"
                    self._transition(bar.timestamp, ISXPhase.WAIT_AOI, ISXPhase.INVALIDATED, "INVALIDATED")
                    return self._decision(self.state.stand_down_reason)
                fib = self._fib(bar.close)
                if 0.618 <= fib <= 0.790:
                    self.state.aoi_qualified = True
                    self.state.aoi_time = bar.timestamp
                    self.state.phase = ISXPhase.WAIT_S2
                    self._transition(bar.timestamp, ISXPhase.WAIT_AOI, ISXPhase.WAIT_S2, "AOI")
                    break

        if self._invalidated(latest.close):
            old_phase = self.state.phase
            self.state.phase = ISXPhase.INVALIDATED
            self.state.x_ready = False
            self.state.aoi_qualified = False
            self.state.stand_down_reason = "close violated EX/1.0 invalidation boundary"
            self._transition(latest.timestamp, old_phase, ISXPhase.INVALIDATED, "INVALIDATED")
            return self._decision(self.state.stand_down_reason)

        if self.state.aoi_qualified and self.state.phase is ISXPhase.WAIT_S2:
            s2 = self._first_shift_after(m15, self.state.aoi_time, intent, opposite=False)
            if s2 is not None and s2.timestamp > (self.state.aoi_time or s2.timestamp):
                self.state.s2_time = s2.timestamp
                self.state.x_ready = True
                self.state.phase = ISXPhase.EXECUTED
                self.state.stand_down_reason = None
                self._transition(s2.timestamp, ISXPhase.WAIT_S2, ISXPhase.EXECUTED, "X")
                return self._decision("ISX-X execution permission", emit=True)

        self.state.stand_down_reason = "waiting for AOI" if not self.state.aoi_qualified else "waiting for M15 S2"
        return self._decision(self.state.stand_down_reason)

    def _first_shift_after(self, candles, after: datetime, intent: Direction, opposite: bool):
        direction = Direction.BEARISH if intent is Direction.BULLISH else Direction.BULLISH
        if not opposite:
            direction = intent
        pivots = detect_pivots(candles, self.pivot_left, self.pivot_right)
        breaks = detect_breaks(candles, pivots, intent)
        matches = [b for b in breaks if b.timestamp > after and b.direction is direction]
        return matches[0] if matches else None

    def _fib(self, close: float) -> float:
        if self.state.ex is None or self.state.ep is None or self.state.ex == self.state.ep:
            return 0.0
        if self.state.intent is Direction.BULLISH:
            return (self.state.ep - close) / (self.state.ep - self.state.ex)
        return (close - self.state.ep) / (self.state.ex - self.state.ep)

    def _invalidated(self, close: float) -> bool:
        if self.state.invalidation_price is None:
            return False
        if self.state.intent is Direction.BULLISH:
            return close <= self.state.invalidation_price
        if self.state.intent is Direction.BEARISH:
            return close >= self.state.invalidation_price
        return False

    def _stand_down(self, reason: str) -> None:
        if self.state.phase not in (ISXPhase.WAIT_INTENT, ISXPhase.REASSESS):
            self._transition(self.state.last_closed_candle, self.state.phase, ISXPhase.REASSESS, "REASSESS")
        self.state.phase = ISXPhase.REASSESS if self.state.setup_id else ISXPhase.WAIT_INTENT
        self.state.x_ready = False
        self.state.stand_down_reason = reason

    def _transition(self, timestamp, old: ISXPhase, new: ISXPhase, event: str) -> None:
        if timestamp is None:
            return
        transition = Transition(timestamp, old, new, event)
        self.state.last_transition = transition
        self.state.transitions.append(transition)

    @staticmethod
    def _setup_id(direction: Direction, timestamp: datetime) -> str:
        return f"ISX-{direction.value[:1].upper()}-{timestamp.strftime('%Y%m%dT%H%M%S')}"

    def _decision(self, reason: str, emit: bool = False) -> ISXDecision:
        direction = self.state.intent
        return ISXDecision(
            self.state.phase,
            direction,
            "ISX_X" if emit else "STAND_DOWN",
            self.state.setup_id,
            emit,
            f"ISX-X:{self.state.setup_id}" if emit and self.state.setup_id else None,
            reason,
            self.state.observability(),
        )
