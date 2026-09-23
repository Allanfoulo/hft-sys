"""Read-only minute-bar sources for the replay service."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..isx.models import Candle


class MinuteBarsProvider(Protocol):
    def get_bars(self, symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[Candle]:
        """Return completed UTC 1-minute bars in [start, end)."""


def _bar(timestamp: datetime, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(timestamp, open_, high, low, close, True)


def _bucket(timestamp: datetime, opens: float, high: float, low: float, close: float) -> list[Candle]:
    result: list[Candle] = []
    # The fixture is OHLC-first: use the midpoint of the requested range as
    # the bucket's first open so every documented high/low remains valid even
    # when the synthetic bucket gaps from the preceding one.
    bucket_open = (high + low) / 2
    previous = bucket_open
    for minute in range(15):
        progress = minute / 14 if minute else 0.0
        value = bucket_open + (close - bucket_open) * progress
        local_high = max(previous, value) + 0.08
        local_low = min(previous, value) - 0.08
        if minute == 7:
            local_high = max(local_high, high)
            local_low = min(local_low, low)
        result.append(_bar(timestamp + timedelta(minutes=minute), previous, local_high, local_low, value))
        previous = value
    return result


def _fixture_day(day: datetime, day_offset: float) -> list[Candle]:
    """Create a compact deterministic day with one complete ISX trade.

    Six 15-minute bars establish the bullish structure. The following
    1-minute bucket contains a close-confirmed S1, an AOI visit, an aligned
    S2, and a move through the default 4R target.
    """
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    specs = [
        (11, 9, 10),
        (13, 10, 12),
        (11, 8, 9),
        (14, 10, 13),
        (12, 9, 11),
        (15, 10, 14),
    ]
    result: list[Candle] = []
    previous = 10.0 + day_offset
    for index, (high, low, close) in enumerate(specs):
        high += day_offset
        low += day_offset
        close += day_offset
        result.extend(_bucket(start + timedelta(minutes=index * 15), previous, high, low, close))
        previous = close

    closes = [10, 12, 10.5, 11, 8.8, 9.1, 9.4, 10.5, 13.2, 14, 16, 20, 26, 30, 35]
    previous = closes[0] + day_offset
    trigger_start = start + timedelta(minutes=90)
    for index, close in enumerate(closes):
        close += day_offset
        open_ = previous
        high = max(open_, close) + 0.2
        low = min(open_, close) - 0.2
        if index == 1:
            high = 13.0 + day_offset
            low = 11.8 + day_offset
        if index == 2:
            low = 10.2 + day_offset
        result.append(_bar(trigger_start + timedelta(minutes=index), open_, high, low, close))
        previous = close

    tail_start = trigger_start + timedelta(minutes=15)
    result.extend(_bucket(tail_start, previous, 36 + day_offset, 34 + day_offset, 35 + day_offset))
    return result


class FixtureMinuteBarsProvider:
    """Offline, deterministic source used by the UI and fixture tests."""

    def get_bars(self, symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[Candle]:
        if not symbol:
            raise ValueError("symbol is required")
        start = start_utc.astimezone(timezone.utc)
        end = end_utc.astimezone(timezone.utc)
        bars: list[Candle] = []
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = 0.0
        while day < end:
            bars.extend(_fixture_day(day, offset))
            day += timedelta(days=1)
            offset += 0.5
        return [bar for bar in bars if start <= bar.timestamp < end]


class AlpacaMinuteBarsProvider:
    """Alpaca historical 1-minute crypto source with read-only pagination."""

    def __init__(self, client):
        self.client = client

    def get_bars(self, symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[Candle]:
        result: list[Candle] = []
        page_token: str | None = None
        while True:
            page, page_token = self.client.get_historical_minute_bars(
                start_utc=start_utc,
                end_utc=end_utc,
                limit=10000,
                page_token=page_token,
            )
            result.extend(page)
            if not page_token or not page:
                break
        return result
