"""Typed values shared by the deterministic ISX engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class PivotKind(str, Enum):
    HH = "HH"
    HL = "HL"
    LH = "LH"
    LL = "LL"


class BreakKind(str, Enum):
    BOS = "BOS"
    CHOCH = "CHOCH"


class ISXPhase(str, Enum):
    WAIT_INTENT = "WAIT_INTENT"
    WAIT_S1 = "WAIT_S1"
    WAIT_AOI = "WAIT_AOI"
    WAIT_S2 = "WAIT_S2"
    X_READY = "X_READY"
    EXECUTED = "EXECUTED"
    REASSESS = "REASSESS"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    complete: bool = True

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            object.__setattr__(
                self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc)
            )


@dataclass(frozen=True)
class Pivot:
    index: int
    timestamp: datetime
    side: str
    price: float
    structural_close: float
    kind: PivotKind


@dataclass(frozen=True)
class StructuralBreak:
    index: int
    timestamp: datetime
    direction: Direction
    kind: BreakKind
    level: float
    close: float
    pivot: Pivot


@dataclass(frozen=True)
class RangeAnchors:
    ex: float
    px: float
    ep: float
    intent_time: datetime
    ex_time: datetime
    px_time: datetime
    ep_time: datetime


@dataclass(frozen=True)
class Transition:
    timestamp: datetime
    from_phase: ISXPhase
    to_phase: ISXPhase
    event: str


@dataclass
class ISXState:
    setup_id: str | None = None
    phase: ISXPhase = ISXPhase.WAIT_INTENT
    h4_direction: Direction = Direction.NEUTRAL
    h1_direction: Direction = Direction.NEUTRAL
    intent: Direction = Direction.NEUTRAL
    ex: float | None = None
    px: float | None = None
    ep: float | None = None
    s1_time: datetime | None = None
    fib_retracement: float | None = None
    aoi_qualified: bool = False
    aoi_time: datetime | None = None
    s2_time: datetime | None = None
    x_ready: bool = False
    invalidation_price: float | None = None
    stand_down_reason: str | None = None
    last_closed_candle: datetime | None = None
    last_transition: Transition | None = None
    transitions: list[Transition] = field(default_factory=list)

    def observability(self) -> dict:
        return {
            "isx_setup_id": self.setup_id,
            "isx_phase": self.phase.value,
            "h4_direction": self.h4_direction.value,
            "h1_direction": self.h1_direction.value,
            "intent": self.intent.value,
            "ex": self.ex,
            "px": self.px,
            "ep": self.ep,
            "s1_time": self.s1_time.isoformat() if self.s1_time else None,
            "fib_retracement": self.fib_retracement,
            "aoi_qualified": self.aoi_qualified,
            "s2_time": self.s2_time.isoformat() if self.s2_time else None,
            "x_ready": self.x_ready,
            "invalidation_price": self.invalidation_price,
            "stand_down_reason": self.stand_down_reason,
            "isx_transition": self.last_transition.event if self.last_transition else None,
            "isx_transition_time": (
                self.last_transition.timestamp.isoformat()
                if self.last_transition
                else None
            ),
        }


@dataclass(frozen=True)
class ISXDecision:
    phase: ISXPhase
    direction: Direction
    action: str
    setup_id: str | None
    x_ready: bool
    execution_tag: str | None
    reason: str
    observability: dict

