"""Offline replay command for the London sweep execution model."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

from .execution.sweep import (
    DEFAULT_EXECUTION_MODEL_TAG,
    SweepPosition,
    build_trade_plan,
)
from .market_structure import Bar, confirmed_fractals, detect_sweeps
from .session_strategy import LondonSweepEngine, jev_allows_entry

REPLAY_SYMBOL = "BTC/USD"


def _bar_payload(bar: Bar) -> dict:
    return {
        "timestamp": bar.start_ts,
        "timestamp_utc": datetime.fromtimestamp(bar.start_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "interval_s": bar.interval_s,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _utc_start(day: date, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).timestamp()


def _bar(start_ts: float, interval_s: int, values: tuple[float, float, float, float]) -> Bar:
    return Bar(start_ts, interval_s, *values)


def replay_day(day: date, execution_model_tag: str = DEFAULT_EXECUTION_MODEL_TAG) -> dict:
    """Replay one deterministic long setup using timestamps inside London.

    This returns structured data so the dashboard and the CLI consume exactly
    the same replay result.
    """
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
        return {"ok": False, "date": day.isoformat(), "error": "fixture did not establish a long London bias"}

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
        return {"ok": False, "date": day.isoformat(), "error": "fixture did not produce a 5s entry signal"}

    answers = {
        "regime": {"choice": "normal"},
        "execution_health": {"score": 2.5},
        "quote_environment": {"score": 1.5},
        "direction": {"choice": "up", "confidence": 0.8},
    }
    allowed, reason = jev_allows_entry(answers, signal.direction)
    if not allowed:
        return {"ok": False, "date": day.isoformat(), "error": f"Jev vetoed fixture: {reason}"}
    plan = build_trade_plan(signal, execution_model_tag=execution_model_tag)
    position = SweepPosition(plan)
    # The fixture deliberately includes both paths so a range replay tests
    # loss handling as well as target handling. This is still simulated data,
    # never a claim about the market on that date.
    loss_fixture = day.toordinal() % 5 == 0
    if loss_fixture:
        transitions = [
            position.update(_bar(one_minute_start + 8 * 60, 5, (101, 101.5, 97.8, 99))),
        ]
    else:
        transitions = [
            position.update(_bar(one_minute_start + 8 * 60, 5, (101, 104.2, 103.5, 104))),
            position.update(_bar(one_minute_start + 9 * 60, 5, (104, 108.6, 106.5, 108))),
            position.update(_bar(one_minute_start + 10 * 60, 5, (108, 110.1, 108.1, 110))),
        ]
    if transitions[-1].state.status != "closed":
        return {"ok": False, "date": day.isoformat(), "error": "fixture did not close at target"}

    lifecycle = [
        {
            "event": transition.event,
            "timestamp": transition.timestamp,
            "timestamp_utc": datetime.fromtimestamp(transition.timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "price": transition.price,
        }
        for transition in transitions
    ]
    return {
        "ok": True,
        "date": day.isoformat(),
        "session": "08:00-11:00 UTC",
        "symbol": REPLAY_SYMBOL,
        "execution_model_tag": plan.execution_model_tag,
        "bias": engine.snapshot.bias,
        "setup_id": setup.setup_id,
        "signal_ts": signal.timestamp,
        "entry_ts": signal.timestamp,
        "direction": plan.direction,
        "quantity": plan.quantity,
        "entry_price": plan.entry_price,
        "stop_price": plan.stop_price,
        "target_price": plan.target_price,
        "exit_ts": transitions[-1].timestamp,
        "exit_price": transitions[-1].state.exit_price,
        "max_loss_usd": plan.max_loss_usd,
        "outcome": transitions[-1].event,
        "profit_loss": "Loss" if transitions[-1].event == "stop" else "Profit",
        "simulated": True,
        "r_multiple": -1.0 if transitions[-1].event == "stop" else 3.0,
        "pnl_usd": plan.max_loss_usd * (-1.0 if transitions[-1].event == "stop" else 3.0),
        "transitions": [transition.event for transition in transitions],
        "lifecycle": lifecycle,
        "chart_bars": [_bar_payload(bar) for bar in fifteen + one_minute + [refinement, trigger]],
        "intent_ts": engine.snapshot.bias_sweep.timestamp if engine.snapshot.bias_sweep else None,
        "intent_price": engine.snapshot.bias_sweep.level if engine.snapshot.bias_sweep else None,
        "s1_ts": setup.sweep.timestamp,
        "s1_price": setup.sweep.level,
        "aoi_ts": setup.refinement.start_ts,
        "aoi_price": setup.trigger_price,
    }


def replay_range(
    start: date,
    end: date,
    execution_model_tag: str = DEFAULT_EXECUTION_MODEL_TAG,
) -> dict:
    """Replay an inclusive UTC date range using the deterministic fixture."""
    if not execution_model_tag.strip():
        raise ValueError("execution model tag must not be blank")
    if end < start:
        raise ValueError("end date must be on or after start date")
    if (end - start).days > 90:
        raise ValueError("date range cannot exceed 91 days")
    rows = [
        replay_day(start + timedelta(days=offset), execution_model_tag)
        for offset in range((end - start).days + 1)
    ]
    successful = [row for row in rows if row.get("ok")]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "execution_model_tag": execution_model_tag,
        "fixture": True,
        "historical": False,
        "simulated": True,
        "rows": rows,
        "summary": {
            "sessions": len(rows),
            "setups": len(successful),
            "trades": len(successful),
            "wins": sum(1 for row in successful if row["r_multiple"] > 0),
            "losses": sum(1 for row in successful if row["r_multiple"] <= 0),
            "total_r": sum(row["r_multiple"] for row in successful),
            "pnl_usd": sum(row["pnl_usd"] for row in successful),
        },
    }


def run_demo(day: date, execution_model_tag: str = DEFAULT_EXECUTION_MODEL_TAG) -> int:
    """Print one deterministic replay result for terminal use."""
    result = replay_day(day, execution_model_tag)
    if not result.get("ok"):
        print(f"replay failed: {result['error']}")
        return 1

    print(f"execution_model_tag: {result['execution_model_tag']}")
    print(f"fixture session: {day.isoformat()} 08:00-11:00 UTC")
    print(f"bias: {result['bias']} | setup: {result['setup_id']} | signal: {result['signal_ts']:.0f}")
    print(
        f"plan: {result['direction']} {result['quantity']:.8f} @ {result['entry_price']:.2f} "
        f"stop {result['stop_price']:.2f} target {result['target_price']:.2f} "
        f"max_loss ${result['max_loss_usd']:.2f}"
    )
    print(
        f"timing: entry {result['entry_ts']:.0f} @ {result['entry_price']:.2f} "
        f"exit {result['exit_ts']:.0f} @ {result['exit_price']:.2f}"
    )
    print("lifecycle: " + " -> ".join(result["transitions"]))
    print(
        f"simulated result: {result['profit_loss']} {result['r_multiple']:+.1f}R "
        f"(${result['pnl_usd']:+.2f})"
    )
    print("replay passed: structure, Jev filter, tagged plan, and exit path")
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
