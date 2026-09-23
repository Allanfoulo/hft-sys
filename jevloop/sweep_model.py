"""Replayable multi-timeframe adapter for the London sweep components.

``SweepExecutionModel`` is deliberately broker-neutral.  A caller feeds it
trades, supplies Jev's already-computed answers when a trigger appears, and
gets back a bounded :class:`~jevloop.execution.sweep.TradePlan`.  The existing
Alpaca client can remain the only code that submits paper orders.
"""

from __future__ import annotations

from dataclasses import dataclass

from .execution.sweep import SweepPosition, SweepRiskConfig, TradePlan, build_trade_plan
from .market_structure import Bar, BarAggregator, Sweep, confirmed_fractals, detect_sweeps
from .session_strategy import EntrySignal, LondonSweepEngine, SessionConfig, jev_allows_entry


@dataclass(frozen=True)
class ModelEvent:
    kind: str
    signal: EntrySignal | None = None
    plan: TradePlan | None = None
    reason: str | None = None


class SweepExecutionModel:
    """Feed trades into 15m/1m/5s structure and produce one-shot plans."""

    def __init__(
        self,
        *,
        session: SessionConfig | None = None,
        risk: SweepRiskConfig | None = None,
        max_history: int = 500,
    ):
        self.session = LondonSweepEngine(session)
        self.risk = risk or SweepRiskConfig()
        self.max_history = max_history
        self._aggregators = {900: BarAggregator(900), 60: BarAggregator(60), 5: BarAggregator(5)}
        self._bars: dict[int, list[Bar]] = {900: [], 60: [], 5: []}
        self._seen_sweeps: dict[int, set[tuple[str, float, float]]] = {900: set(), 60: set()}
        self.position = None

    def accept_plan(self, plan: TradePlan) -> None:
        """Mark a returned plan as accepted after the broker confirms entry."""
        if self.position is not None:
            raise RuntimeError("only one sweep position may be open")
        self.position = SweepPosition(plan, self.risk)

    def _remember(self, interval: int, bar: Bar) -> None:
        history = self._bars[interval]
        history.append(bar)
        if len(history) > self.max_history:
            del history[: len(history) - self.max_history]

    def _new_sweeps(self, interval: int) -> list[Sweep]:
        bars = self._bars[interval]
        fractals = confirmed_fractals(bars)
        found = []
        seen = self._seen_sweeps[interval]
        for sweep in detect_sweeps(bars, fractals):
            key = (sweep.direction, sweep.bar.start_ts, sweep.fractal.level)
            if key not in seen:
                seen.add(key)
                found.append(sweep)
        return found

    def feed_trade(
        self,
        ts: float,
        price: float,
        volume: float = 0.0,
        *,
        jev_answers: dict | None = None,
        inventory_qty: float = 0.0,
        stale: bool = False,
        late: bool = False,
    ) -> list[ModelEvent]:
        """Feed one trade and return any structure or entry events it closes."""
        events: list[ModelEvent] = []
        for interval in (900, 60, 5):
            closed = self._aggregators[interval].push(ts, price, volume)
            for bar in closed:
                self._remember(interval, bar)
                if interval == 900:
                    for sweep in self._new_sweeps(interval):
                        self.session.on_15m_sweep(sweep)
                        events.append(ModelEvent("15m_sweep", reason=sweep.direction))
                elif interval == 60:
                    one_min_sweeps = self._new_sweeps(interval)
                    for sweep in one_min_sweeps:
                        self.session.on_1m_sweep(sweep, position_open=self.position is not None)
                        events.append(ModelEvent("1m_sweep", reason=sweep.direction))
                    self.session.on_1m_bar(bar, position_open=self.position is not None)
                else:
                    if self.position is not None:
                        transition = self.position.update(bar)
                        if transition.event != "hold":
                            events.append(ModelEvent("position_" + transition.event))
                        if transition.state.status == "closed":
                            self.position = None
                    signal = self.session.on_5s_bar(bar, position_open=self.position is not None)
                    if signal is None:
                        continue
                    if jev_answers is None:
                        events.append(ModelEvent("entry_veto", signal=signal, reason="Jev answers required"))
                        continue
                    allowed, reason = jev_allows_entry(jev_answers, signal.direction, stale=stale, late=late)
                    if not allowed:
                        events.append(ModelEvent("entry_veto", signal=signal, reason=reason))
                        continue
                    try:
                        plan = build_trade_plan(signal, self.risk, inventory_qty=inventory_qty)
                    except ValueError as exc:
                        events.append(ModelEvent("entry_rejected", signal=signal, reason=str(exc)))
                        continue
                    events.append(ModelEvent("entry_plan", signal=signal, plan=plan))
        return events
