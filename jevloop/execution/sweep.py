"""Paper-safe trade planning and lifecycle for the London sweep model.

This module owns levels and state transitions, but it does not submit orders.
The existing Alpaca client and :mod:`jevloop.risk` remain the final execution
and hard-limit gates when this model is wired into the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..market_structure import Bar
from ..session_strategy import EntrySignal


class TradePlanError(ValueError):
    """The signal cannot be converted into a bounded spot trade."""


class ShortInventoryError(TradePlanError):
    """A short signal was received without sellable spot inventory."""


@dataclass(frozen=True)
class SweepRiskConfig:
    max_risk_usd: float = 5.0
    target_r: float = 3.0
    breakeven_r: float = 1.0
    profit_lock_trigger_r: float = 2.5
    profit_lock_r: float = 2.0
    max_order_notional_usd: float = 25.0
    max_position_usd: float = 50.0

    def __post_init__(self) -> None:
        if self.max_risk_usd <= 0:
            raise ValueError("max_risk_usd must be positive")
        if not 0 < self.breakeven_r < self.profit_lock_trigger_r < self.target_r:
            raise ValueError("R thresholds must satisfy 0 < breakeven < lock < target")
        if not 0 < self.profit_lock_r < self.target_r:
            raise ValueError("profit_lock_r must be between zero and target_r")
        if self.max_order_notional_usd <= 0 or self.max_position_usd <= 0:
            raise ValueError("notional caps must be positive")


@dataclass(frozen=True)
class TradePlan:
    setup_id: int
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: float
    risk_per_unit: float
    max_loss_usd: float


def build_trade_plan(
    signal: EntrySignal,
    config: SweepRiskConfig | None = None,
    *,
    inventory_qty: float = 0.0,
) -> TradePlan:
    """Build a capped 3R plan from a deterministic entry signal."""
    config = config or SweepRiskConfig()
    if signal.direction not in {"long", "short"}:
        raise TradePlanError(f"unknown direction: {signal.direction!r}")
    entry = float(signal.entry_price)
    stop = float(signal.stop_price)
    if entry <= 0 or stop <= 0:
        raise TradePlanError("entry and stop must be positive")
    if signal.direction == "long":
        if stop >= entry:
            raise TradePlanError("long stop must be below entry")
    else:
        if stop <= entry:
            raise TradePlanError("short stop must be above entry")
        if inventory_qty <= 0:
            raise ShortInventoryError("short signal has no sellable spot inventory")

    distance = abs(entry - stop)
    max_qty = min(
        config.max_risk_usd / distance,
        config.max_order_notional_usd / entry,
        config.max_position_usd / entry,
    )
    if signal.direction == "short":
        max_qty = min(max_qty, inventory_qty)
    if max_qty <= 0:
        raise TradePlanError("risk caps leave no executable quantity")
    target = (
        entry + config.target_r * distance
        if signal.direction == "long"
        else entry - config.target_r * distance
    )
    return TradePlan(
        setup_id=signal.setup_id,
        direction=signal.direction,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=max_qty,
        risk_per_unit=distance,
        max_loss_usd=max_qty * distance,
    )


@dataclass(frozen=True)
class PositionState:
    plan: TradePlan
    current_stop: float
    status: str = "open"  # open, closed
    exit_price: float | None = None
    exit_reason: str | None = None
    best_r: float = 0.0


@dataclass(frozen=True)
class PositionTransition:
    event: str  # hold, breakeven, profit_lock, target, stop
    timestamp: float
    price: float | None
    state: PositionState


class SweepPosition:
    """Manage one spot position with conservative bar ordering."""

    def __init__(self, plan: TradePlan, config: SweepRiskConfig | None = None):
        self.config = config or SweepRiskConfig()
        self.state = PositionState(plan, plan.stop_price)

    def _r(self, price: float) -> float:
        signed = price - self.state.plan.entry_price
        if self.state.plan.direction == "short":
            signed = -signed
        return signed / self.state.plan.risk_per_unit

    def _close(self, timestamp: float, price: float, reason: str) -> PositionTransition:
        self.state = replace(
            self.state,
            status="closed",
            exit_price=price,
            exit_reason=reason,
            best_r=max(self.state.best_r, self._r(price)),
        )
        return PositionTransition(reason, timestamp, price, self.state)

    def update(self, bar: Bar) -> PositionTransition:
        """Process one bar; stop is checked before target on ambiguous bars."""
        if self.state.status == "closed":
            return PositionTransition("hold", bar.end_ts, None, self.state)
        plan = self.state.plan
        favourable = bar.high if plan.direction == "long" else bar.low
        adverse = bar.low if plan.direction == "long" else bar.high
        self.state = replace(self.state, best_r=max(self.state.best_r, self._r(favourable)))

        stop_hit = adverse <= self.state.current_stop if plan.direction == "long" else adverse >= self.state.current_stop
        if stop_hit:
            return self._close(bar.end_ts, self.state.current_stop, "stop")

        current_r = self._r(favourable)
        event: str | None = None
        if current_r >= self.config.profit_lock_trigger_r:
            locked = (
                plan.entry_price + self.config.profit_lock_r * plan.risk_per_unit
                if plan.direction == "long"
                else plan.entry_price - self.config.profit_lock_r * plan.risk_per_unit
            )
            improves = locked > self.state.current_stop if plan.direction == "long" else locked < self.state.current_stop
            if improves:
                self.state = replace(self.state, current_stop=locked)
                event = "profit_lock"

        if current_r >= self.config.breakeven_r:
            improves = plan.entry_price > self.state.current_stop if plan.direction == "long" else plan.entry_price < self.state.current_stop
            if improves:
                self.state = replace(self.state, current_stop=plan.entry_price)
                event = event or "breakeven"

        target_hit = favourable >= plan.target_price if plan.direction == "long" else favourable <= plan.target_price
        if target_hit:
            return self._close(bar.end_ts, plan.target_price, "target")
        if event is not None:
            return PositionTransition(event, bar.end_ts, None, self.state)
        return PositionTransition("hold", bar.end_ts, None, self.state)
