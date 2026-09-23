from datetime import datetime, timezone

from jevloop.market_structure import Bar, BarAggregator, Fractal, confirmed_fractals, detect_sweeps


def ts(hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(2026, 1, 5, hour, minute, second, tzinfo=timezone.utc).timestamp()


def test_aggregator_emits_fixed_utc_bars_and_flushes():
    agg = BarAggregator(5)
    assert agg.push(ts(8, 0, 1), 100.0) == []
    assert agg.push(ts(8, 0, 4), 101.0) == []
    closed = agg.push(ts(8, 0, 6), 99.0)
    assert closed[0].open == 100.0
    assert closed[0].high == 101.0
    assert closed[0].low == 100.0
    assert closed[0].close == 101.0
    assert agg.flush().close == 99.0


def test_confirmed_fractal_requires_unique_extreme():
    bars = [Bar(i * 60, 60, 100 + i, 100 + i, 99 + i, 100 + i) for i in range(5)]
    bars[2] = Bar(120, 60, 103, 110, 98, 104)
    found = confirmed_fractals(bars)
    assert [(f.kind, f.index) for f in found] == [("high", 2), ("low", 2)]


def test_sweep_requires_wick_through_and_close_back_inside():
    fractal = Fractal("low", 0, 100.0, 60.0)
    wick_only = Bar(120, 60, 101, 103, 99, 99.0)
    valid = Bar(180, 60, 101, 103, 99, 101.0)
    sweeps = detect_sweeps([wick_only, valid], [fractal])
    assert len(sweeps) == 1
    assert sweeps[0].direction == "long"
