"""Deterministic candle aggregation, fractals, and liquidity sweeps.

The execution model consumes trades, not fabricated candles.  This module is
deliberately independent of Alpaca and Jev so historical fixtures can replay
the exact same structure decisions as the live loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class Bar:
    start_ts: float
    interval_s: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def end_ts(self) -> float:
        return self.start_ts + self.interval_s


class BarAggregator:
    """Build fixed UTC-aligned OHLC bars from timestamped trades."""

    def __init__(self, interval_s: int):
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.interval_s = interval_s
        self._current: Bar | None = None

    def push(self, ts: float, price: float, volume: float = 0.0) -> list[Bar]:
        if price <= 0:
            raise ValueError("price must be positive")
        bucket = floor(ts / self.interval_s) * self.interval_s
        if self._current is None:
            self._current = Bar(bucket, self.interval_s, price, price, price, price, volume)
            return []
        if bucket < self._current.start_ts:
            return []
        if bucket == self._current.start_ts:
            c = self._current
            self._current = Bar(
                c.start_ts,
                c.interval_s,
                c.open,
                max(c.high, price),
                min(c.low, price),
                price,
                c.volume + volume,
            )
            return []
        closed = self._current
        self._current = Bar(bucket, self.interval_s, price, price, price, price, volume)
        return [closed]

    def flush(self) -> Bar | None:
        closed, self._current = self._current, None
        return closed


@dataclass(frozen=True)
class Fractal:
    kind: str  # "high" or "low"
    index: int
    level: float
    confirmed_at: float


def confirmed_fractals(bars: list[Bar], left: int = 2, right: int = 2) -> list[Fractal]:
    """Return strict confirmed fractals, excluding the unconfirmed edge bars."""
    if left < 1 or right < 1:
        raise ValueError("left and right must be positive")
    found: list[Fractal] = []
    for i in range(left, len(bars) - right):
        window = bars[i - left : i + right + 1]
        center = bars[i]
        highs = [b.high for b in window]
        lows = [b.low for b in window]
        if center.high == max(highs) and highs.count(center.high) == 1:
            found.append(Fractal("high", i, center.high, bars[i + right].end_ts))
        if center.low == min(lows) and lows.count(center.low) == 1:
            found.append(Fractal("low", i, center.low, bars[i + right].end_ts))
    return found


@dataclass(frozen=True)
class Sweep:
    direction: str  # "long" for a low sweep, "short" for a high sweep
    bar: Bar
    fractal: Fractal

    @property
    def timestamp(self) -> float:
        return self.bar.end_ts

    @property
    def level(self) -> float:
        return self.fractal.level

    @property
    def extreme(self) -> float:
        return self.bar.low if self.direction == "long" else self.bar.high


def detect_sweeps(bars: list[Bar], fractals: list[Fractal]) -> list[Sweep]:
    """Detect wick-through-and-close-back-inside sweeps.

    A low sweep creates a long bias; a high sweep creates a short bias.  The
    fractal must have been confirmed before the sweeping candle opened.
    """
    found: list[Sweep] = []
    for i, bar in enumerate(bars):
        eligible = [f for f in fractals if f.index < i and f.confirmed_at <= bar.start_ts]
        highs = [f for f in eligible if f.kind == "high" and bar.high > f.level and bar.close < f.level]
        lows = [f for f in eligible if f.kind == "low" and bar.low < f.level and bar.close > f.level]
        if highs:
            found.append(Sweep("short", bar, max(highs, key=lambda f: f.index)))
        if lows:
            found.append(Sweep("long", bar, max(lows, key=lambda f: f.index)))
    return found
