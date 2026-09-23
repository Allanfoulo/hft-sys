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
    # Fractals are confirmed in timestamp order. Keep the latest eligible
    # level for each side instead of rebuilding a full eligible list for every
    # bar; the historical replay can otherwise turn a month of 1m bars into an
    # O(n²) scan.
    ordered = sorted(fractals, key=lambda item: (item.confirmed_at, item.index))
    cursor = 0
    pending_by_index: dict[int, list[Fractal]] = {}
    latest_high: Fractal | None = None
    latest_low: Fractal | None = None
    found: list[Sweep] = []
    for i, bar in enumerate(bars):
        # A fractal becomes time-eligible when its confirmation timestamp is
        # reached, then index-eligible once the sweeping bar is later than its
        # center. The small pending map preserves both conditions without a
        # quadratic eligible-list rebuild.
        while cursor < len(ordered) and ordered[cursor].confirmed_at <= bar.start_ts:
            fractal = ordered[cursor]
            if fractal.index < i:
                eligible = [fractal]
            else:
                pending_by_index.setdefault(fractal.index, []).append(fractal)
                eligible = []
            for item in eligible:
                if item.kind == "high" and (latest_high is None or item.index > latest_high.index):
                    latest_high = item
                elif item.kind == "low" and (latest_low is None or item.index > latest_low.index):
                    latest_low = item
            cursor += 1
        for fractal in pending_by_index.pop(i - 1, []):
            if fractal.kind == "high" and (latest_high is None or fractal.index > latest_high.index):
                latest_high = fractal
            elif fractal.kind == "low" and (latest_low is None or fractal.index > latest_low.index):
                latest_low = fractal
        if latest_high is not None and bar.high > latest_high.level and bar.close < latest_high.level:
            found.append(Sweep("short", bar, latest_high))
        if latest_low is not None and bar.low < latest_low.level and bar.close > latest_low.level:
            found.append(Sweep("long", bar, latest_low))
    return found
