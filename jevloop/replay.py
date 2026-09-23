"""Offline replay command for the London sweep execution model."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

from .execution.sweep import (
    DEFAULT_EXECUTION_MODEL_TAG,
    SweepPosition,
    build_trade_plan,
)
from .market_structure import Bar, confirmed_fractals, detect_sweeps
from .session_strategy import LondonSweepEngine, jev_allows_entry


def _utc_start(day: date, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).timestamp()


def _bar(start_ts: float, interval_s: int, values: tuple[float, float, float, float]) -> Bar:
    return Bar(start_ts, interval_s, *values)


def run_demo(day: date, execution_model_tag: str = DEFAULT_EXECUTION_MODEL_TAG) -> int:
    """Replay one deterministic long setup using timestamps inside London."""
    session_day = _utc_start(day, 0)
    pre_session = session_day + 6 * 3600
    fifteen_values = [
        (100, 102, 99, 101),
        (101, 103, 100, 102),
        (102, 104, 98, 101),
        (101, 103, 100, 102),
        (102, 103, 101, 103),
        (103, 104, 100, 102),
        (102, 103, 97, 101),  # low sweep before the 08:00 UTC session
    ]
    fifteen = [
        _bar(pre_session + 15 * 60 * i, 900, values)
        for i, values in enumerate(fifteen_values)
    ]
    fifteen_sweeps = detect_sweeps(fifteen, confirmed_fractals(fifteen))
    engine = LondonSweepEngine()
    for sweep in fifteen_sweeps:
        engine.on_15m_sweep(sweep)
    if engine.snapshot.bias != "long":
        print("replay failed: fixture did not establish a long London bias")
        return 1

    one_minute_start = session_day + 8 * 3600
    one_minute_values = [
        (100, 101, 100, 100.5),
        (100.5, 101.5, 100.2, 101),
        (101, 102, 99, 100.5),
        (100.5, 101.5, 100.2, 101),
        (101, 102, 100.2, 101),
        (101, 102, 98, 100.3),  # low sweep in the active direction
    ]
    one_minute = [
        _bar(one_minute_start + 60 * i, 60, values)
        for i, values in enumerate(one_minute_values)
    ]
    one_minute_sweeps = detect_sweeps(one_minute, confirmed_fractals(one_minute))
    for sweep in one_minute_sweeps:
        engine.on_1m_sweep(sweep)
    refinement = _bar(one_minute_start + 6 * 60, 60, (100.3, 101, 99.8, 100.5))
    setup = engine.on_1m_bar(refinement)
    trigger = _bar(one_minute_start + 7 * 60, 5, (100.8, 101.2, 100.7, 101.1))
    signal = engine.on_5s_bar(trigger)
    if setup is None or signal is None:
        print("replay failed: fixture did not produce a 5s entry signal")
        return 1

    answers = {
        "regime": {"choice": "normal"},
        "execution_health": {"score": 2.5},
        "quote_environment": {"score": 1.5},
        "direction": {"choice": "up", "confidence": 0.8},
    }
    allowed, reason = jev_allows_entry(answers, signal.direction)
    if not allowed:
        print(f"replay failed: Jev vetoed fixture: {reason}")
        return 1
    plan = build_trade_plan(signal, execution_model_tag=execution_model_tag)
    position = SweepPosition(plan)
    transitions = [
        position.update(_bar(one_minute_start + 8 * 60, 5, (101, 104.2, 103.5, 104))),
        position.update(_bar(one_minute_start + 9 * 60, 5, (104, 108.6, 106.5, 108))),
        position.update(_bar(one_minute_start + 10 * 60, 5, (108, 110.1, 108.1, 110))),
    ]
    if transitions[-1].state.status != "closed":
        print("replay failed: fixture did not close at target")
        return 1

    print(f"execution_model_tag: {plan.execution_model_tag}")
    print(f"fixture session: {day.isoformat()} 08:00-11:00 UTC")
    print(f"bias: {engine.snapshot.bias} | setup: {setup.setup_id} | signal: {signal.timestamp:.0f}")
    print(
        f"plan: {plan.direction} {plan.quantity:.8f} @ {plan.entry_price:.2f} "
        f"stop {plan.stop_price:.2f} target {plan.target_price:.2f} "
        f"max_loss ${plan.max_loss_usd:.2f}"
    )
    print("lifecycle: " + " -> ".join(t.event for t in transitions))
    print("replay passed: structure, Jev filter, tagged plan, and exits")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop replay-sweep")
    parser.add_argument(
        "--date",
        default="2026-09-23",
        help="UTC fixture date (YYYY-MM-DD); it does not use the wall clock",
    )
    parser.add_argument("--tag", default=DEFAULT_EXECUTION_MODEL_TAG)
    args = parser.parse_args(argv)
    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    return run_demo(day, args.tag)
