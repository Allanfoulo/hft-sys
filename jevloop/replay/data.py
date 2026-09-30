"""Read-only minute-bar sources for the replay service."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import importlib
import os
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


def _fixture_day(
    day: datetime,
    day_offset: float,
    *,
    price_offset: float = 0.0,
    price_scale: float = 1.0,
) -> list[Candle]:
    """Create a compact deterministic day with one complete ISX trade.

    Six 15-minute bars establish the bullish structure. The following
    1-minute bucket contains a close-confirmed S1, an AOI visit, an aligned
    S2, and a move through the default 4R target.
    """
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    def price(value: float) -> float:
        return price_offset + (value + day_offset) * price_scale

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
        high = price(high)
        low = price(low)
        close = price(close)
        result.extend(_bucket(start + timedelta(minutes=index * 15), previous, high, low, close))
        previous = close

    closes = [10, 12, 10.5, 11, 8.8, 9.1, 9.4, 10.5, 13.2, 14, 16, 20, 26, 30, 35]
    previous = price(closes[0])
    trigger_start = start + timedelta(minutes=90)
    for index, close in enumerate(closes):
        close = price(close)
        open_ = previous
        high = max(open_, close) + (0.2 * price_scale)
        low = min(open_, close) - (0.2 * price_scale)
        if index == 1:
            high = price(13.0)
            low = price(11.8)
        if index == 2:
            low = price(10.2)
        result.append(_bar(trigger_start + timedelta(minutes=index), open_, high, low, close))
        previous = close

    tail_start = trigger_start + timedelta(minutes=15)
    result.extend(_bucket(tail_start, previous, price(36), price(34), price(35)))
    return result


class FixtureMinuteBarsProvider:
    """Offline, deterministic source used by the UI and fixture tests.

    ``XAUUSD`` is deliberately synthetic and is priced in a gold-like range
    so chart scales and stops look like the MT5 instrument. It is not a claim
    about broker history; use the MT5 source for that.
    """

    def get_bars(self, symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[Candle]:
        if not symbol:
            raise ValueError("symbol is required")
        start = start_utc.astimezone(timezone.utc)
        end = end_utc.astimezone(timezone.utc)
        bars: list[Candle] = []
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = 0.0
        is_gold = symbol.strip().upper().replace("/", "") == "XAUUSD"
        while day < end:
            bars.extend(
                _fixture_day(
                    day,
                    offset,
                    price_offset=2300.0 if is_gold else 0.0,
                )
            )
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


class MT5MinuteBarsProvider:
    """Read completed 1-minute bars from a connected MetaTrader 5 terminal.

    The dependency is optional so fixture and Alpaca replay remain usable on
    machines without MT5. The provider is read-only: it calls ``copy_rates_range``
    and never sends orders.
    """

    def __init__(self, mt5_module=None, terminal_path: str | None = None):
        self._mt5_module = mt5_module
        self.terminal_path = terminal_path or os.environ.get("MT5_TERMINAL_PATH")

    def _module(self):
        if self._mt5_module is None:
            try:
                self._mt5_module = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise RuntimeError(
                    "MetaTrader5 Python package is not installed; install the optional mt5 dependency"
                ) from exc
        return self._mt5_module

    def get_bars(self, symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[Candle]:
        mt5 = self._module()
        initialize_kwargs = {"path": self.terminal_path} if self.terminal_path else {}
        initialized = bool(mt5.initialize(**initialize_kwargs))
        if not initialized:
            last_error = getattr(mt5, "last_error", lambda: "unknown MT5 initialization error")()
            raise RuntimeError(f"MT5 terminal could not be initialized: {last_error}")
        try:
            if hasattr(mt5, "symbol_select") and not mt5.symbol_select(symbol, True):
                last_error = getattr(mt5, "last_error", lambda: "symbol unavailable")()
                raise RuntimeError(f"MT5 symbol {symbol} is unavailable: {last_error}")
            timeframe = getattr(mt5, "TIMEFRAME_M1", 1)
            rates = mt5.copy_rates_range(
                symbol,
                timeframe,
                start_utc.astimezone(timezone.utc),
                end_utc.astimezone(timezone.utc),
            )
            if rates is None:
                last_error = getattr(mt5, "last_error", lambda: "no rates returned")()
                raise RuntimeError(f"MT5 historical bars unavailable: {last_error}")
            bars: list[Candle] = []
            for row in rates:
                raw_time = row["time"]
                timestamp = (
                    raw_time.astimezone(timezone.utc)
                    if isinstance(raw_time, datetime)
                    else datetime.fromtimestamp(float(raw_time), timezone.utc)
                )
                if not start_utc <= timestamp < end_utc:
                    continue
                bars.append(
                    _bar(
                        timestamp,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                    )
                )
            return sorted(bars, key=lambda bar: bar.timestamp)
        finally:
            mt5.shutdown()
