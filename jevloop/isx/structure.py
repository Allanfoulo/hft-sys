"""Close-confirmed pivot and break detection for ISX.

Pivots use wick highs/lows for identification, but every structural level is
the close of the confirmed pivot candle. Incomplete candles are discarded at
the boundary and never participate in a pivot or break.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .models import (
    BreakKind,
    Candle,
    Direction,
    Pivot,
    PivotKind,
    RangeAnchors,
    StructuralBreak,
)


def completed_candles(candles: Iterable[Candle]) -> list[Candle]:
    """Return unique, completed candles in timestamp order."""
    by_time = {c.timestamp: c for c in candles if c.complete}
    return [by_time[t] for t in sorted(by_time)]


def detect_pivots(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[Pivot]:
    bars = completed_candles(candles)
    if left < 1 or right < 1:
        raise ValueError("pivot strength must be at least one candle on each side")
    pivots: list[Pivot] = []
    prior_high: Pivot | None = None
    prior_low: Pivot | None = None
    for i in range(left, len(bars) - right):
        bar = bars[i]
        left_bars = bars[i - left : i]
        right_bars = bars[i + 1 : i + right + 1]
        if bar.high > max(x.high for x in left_bars + right_bars):
            kind = PivotKind.HH if prior_high is None or bar.high > prior_high.price else PivotKind.LH
            pivot = Pivot(i, bar.timestamp, "high", bar.high, bar.close, kind)
            pivots.append(pivot)
            prior_high = pivot
        if bar.low < min(x.low for x in left_bars + right_bars):
            kind = PivotKind.LL if prior_low is None or bar.low < prior_low.price else PivotKind.HL
            pivot = Pivot(i, bar.timestamp, "low", bar.low, bar.close, kind)
            pivots.append(pivot)
            prior_low = pivot
    return sorted(pivots, key=lambda p: (p.index, p.side))


def detect_breaks(
    candles: Sequence[Candle],
    pivots: Sequence[Pivot] | None = None,
    established_direction: Direction | None = None,
) -> list[StructuralBreak]:
    bars = completed_candles(candles)
    pivots = list(pivots if pivots is not None else detect_pivots(bars))
    result: list[StructuralBreak] = []
    for pivot in pivots:
        direction = Direction.BULLISH if pivot.side == "high" else Direction.BEARISH
        crossed = (
            lambda bar: bar.close > pivot.structural_close
            if direction is Direction.BULLISH
            else bar.close < pivot.structural_close
        )
        for j in range(pivot.index + 1, len(bars)):
            bar = bars[j]
            if crossed(bar):
                kind = (
                    BreakKind.BOS
                    if established_direction in (None, direction)
                    else BreakKind.CHOCH
                )
                result.append(
                    StructuralBreak(
                        j,
                        bar.timestamp,
                        direction,
                        kind,
                        pivot.structural_close,
                        bar.close,
                        pivot,
                    )
                )
                break
    return sorted(result, key=lambda b: (b.index, b.direction.value))


def structure_direction(candles: Sequence[Candle], left: int = 2, right: int = 2) -> Direction:
    """Infer directional structure only from confirmed pivots and closes."""
    bars = completed_candles(candles)
    pivots = detect_pivots(bars, left, right)
    highs = [p for p in pivots if p.side == "high"]
    lows = [p for p in pivots if p.side == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return Direction.NEUTRAL
    if highs[-1].kind is PivotKind.HH and lows[-1].kind is PivotKind.HL:
        direction = Direction.BULLISH
    elif highs[-1].kind is PivotKind.LH and lows[-1].kind is PivotKind.LL:
        direction = Direction.BEARISH
    else:
        return Direction.NEUTRAL
    breaks = detect_breaks(bars, pivots, direction)
    return direction if any(b.direction is direction and b.kind is BreakKind.BOS for b in breaks) else Direction.NEUTRAL


def latest_intent_break(candles: Sequence[Candle], direction: Direction, left: int = 2, right: int = 2) -> StructuralBreak | None:
    bars = completed_candles(candles)
    pivots = detect_pivots(bars, left, right)
    breaks = detect_breaks(bars, pivots, direction)
    matches = [b for b in breaks if b.direction is direction and b.kind is BreakKind.BOS]
    return matches[-1] if matches else None


def range_anchors(
    candles: Sequence[Candle], intent_break: StructuralBreak, direction: Direction, left: int = 2, right: int = 2
) -> RangeAnchors | None:
    """Find EX/PX/EP on the leg that produced the intent break."""
    bars = completed_candles(candles)
    pivots = detect_pivots(bars, left, right)
    if direction is Direction.BULLISH:
        origins = [p for p in pivots if p.side == "low" and p.index < intent_break.pivot.index]
        if not origins:
            return None
        origin = origins[-1]
        internal = [p for p in pivots if p.side == "low" and origin.index <= p.index <= intent_break.index]
    elif direction is Direction.BEARISH:
        origins = [p for p in pivots if p.side == "high" and p.index < intent_break.pivot.index]
        if not origins:
            return None
        origin = origins[-1]
        internal = [p for p in pivots if p.side == "high" and origin.index <= p.index <= intent_break.index]
    else:
        return None
    px = internal[-1] if internal else origin
    ep = bars[intent_break.index]
    return RangeAnchors(
        ex=origin.price,
        px=px.price,
        ep=ep.close,
        intent_time=intent_break.timestamp,
        ex_time=origin.timestamp,
        px_time=px.timestamp,
        ep_time=ep.timestamp,
    )
