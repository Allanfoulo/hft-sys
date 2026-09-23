"""Historical-bar boundary used by deterministic strategy engines."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from .isx.models import Candle


class HistoricalBarsProvider(Protocol):
    def get_bars(self, timeframe: str, limit: int = 300, end: datetime | None = None) -> Sequence[Candle]:
        """Return completed OHLC candles, newest last, for one timeframe."""


class AlpacaHistoricalBarsProvider:
    """Adapter around the existing Alpaca market-data client.

    The ISX engine depends only on ``HistoricalBarsProvider`` and never knows
    which vendor supplied the candles.
    """

    def __init__(self, client):
        self.client = client

    def get_bars(self, timeframe: str, limit: int = 300, end: datetime | None = None) -> Sequence[Candle]:
        return self.client.get_historical_bars(timeframe, limit=limit, end=end)

