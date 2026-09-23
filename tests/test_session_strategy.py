from datetime import datetime, timezone

from jevloop.market_structure import Bar, Fractal, Sweep
from jevloop.session_strategy import (
    LondonSweepEngine,
    SessionConfig,
    jev_allows_entry,
    session_bounds,
)


def ts(hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(2026, 1, 5, hour, minute, second, tzinfo=timezone.utc).timestamp()


def sweep(direction: str, hour: int, minute: int, extreme: float, level: float = 100.0) -> Sweep:
    start = ts(hour, minute) - 60
    bar = Bar(start, 60, level, level + 2, extreme, level + 1)
    fractal = Fractal("low" if direction == "long" else "high", 0, level, start - 1)
    return Sweep(direction, bar, fractal)


def test_session_bounds_use_same_day_london_window():
    start, end = session_bounds(ts(7, 0))
    assert datetime.fromtimestamp(start, timezone.utc).hour == 8
    assert end - start == 3 * 60 * 60


def test_long_bias_refinement_and_5s_break_trigger_once():
    engine = LondonSweepEngine()
    engine.on_15m_sweep(sweep("long", 7, 45, 98.0))
    one_min_sweep = sweep("long", 8, 10, 99.0)
    assert engine.on_1m_sweep(one_min_sweep) is None
    refinement = Bar(ts(8, 10), 60, 100.0, 102.0, 99.5, 101.0)
    setup = engine.on_1m_bar(refinement)
    assert setup is not None
    assert setup.trigger_price == 102.0
    trigger = Bar(ts(8, 11), 5, 101.5, 102.1, 101.4, 102.0)
    signal = engine.on_5s_bar(trigger)
    assert signal is not None
    assert signal.direction == "long"
    assert engine.on_5s_bar(trigger) is None


def test_opposite_15m_sweep_invalidates_pending_setup():
    engine = LondonSweepEngine()
    engine.on_15m_sweep(sweep("long", 7, 45, 98.0))
    one_min_sweep = sweep("long", 8, 10, 99.0)
    engine.on_1m_sweep(one_min_sweep)
    engine.on_1m_bar(Bar(ts(8, 10), 60, 100, 102, 99.5, 101))
    engine.on_15m_sweep(sweep("short", 8, 20, 103.0))
    assert engine.snapshot.invalidated is True
    assert engine.snapshot.pending_setup is None
    assert engine.on_5s_bar(Bar(ts(8, 21), 5, 101, 103, 100, 102)) is None


def test_trigger_expiry_and_position_gate():
    engine = LondonSweepEngine(SessionConfig(trigger_expiry_s=10))
    engine.on_15m_sweep(sweep("long", 7, 45, 98.0))
    one_min_sweep = sweep("long", 8, 10, 99.0)
    engine.on_1m_sweep(one_min_sweep)
    engine.on_1m_bar(Bar(ts(8, 10), 60, 100, 102, 99.5, 101))
    assert engine.on_5s_bar(Bar(ts(8, 11, 0), 5, 101, 101.5, 100.5, 101), position_open=True) is None
    assert engine.on_5s_bar(Bar(ts(8, 11, 11), 5, 101, 103, 100.5, 102)) is None


def test_jev_filter_is_veto_only():
    answers = {
        "regime": {"choice": "normal"},
        "execution_health": {"score": 2.5},
        "quote_environment": {"score": 1.5},
        "direction": {"choice": "up", "confidence": 0.8},
    }
    assert jev_allows_entry(answers, "long")[0] is True
    assert jev_allows_entry(answers, "short")[0] is False
    assert jev_allows_entry(answers, "long", late=True)[0] is False
