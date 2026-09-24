"""Read-only OHLC data and annotations for a single ISX replay trade."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from ..isx.models import Candle
from ..isx.structure import completed_candles
from .engine import aggregate_15m
from .models import (
    ReplayChartBar,
    ReplayChartLevel,
    ReplayChartMarker,
    ReplayRequest,
    ReplayTrade,
)


def _price_at(bars: Sequence[Candle], timestamp: datetime, fallback: float | None) -> float | None:
    for bar in bars:
        if bar.timestamp == timestamp:
            return bar.close
    return fallback


def _chart_bar(candle: Candle, timeframe: str) -> ReplayChartBar:
    return ReplayChartBar(
        candle.timestamp.astimezone(timezone.utc),
        timeframe,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
    )


def _aoi(trade: ReplayTrade) -> dict[str, float] | None:
    if trade.ex_price is None or trade.ep_price is None or trade.ex_price == trade.ep_price:
        return None
    distance = abs(trade.ep_price - trade.ex_price)
    if trade.side.value == "BUY":
        p618 = trade.ep_price - distance * 0.618
        p705 = trade.ep_price - distance * 0.705
        p790 = trade.ep_price - distance * 0.790
    else:
        p618 = trade.ep_price + distance * 0.618
        p705 = trade.ep_price + distance * 0.705
        p790 = trade.ep_price + distance * 0.790
    return {
        "low": min(p618, p790),
        "high": max(p618, p790),
        "optimal": p705,
        "fib_low": 0.618,
        "fib_high": 0.790,
    }


def build_trade_chart(
    trade: ReplayTrade,
    minute_bars: Sequence[Candle],
    request: ReplayRequest,
) -> dict:
    """Return a bounded, completed-bar chart payload for one trade.

    The window begins one hour before Intent to show the structural lead-in and
    ends fifteen minutes after a closed trade. Open trades run through the
    requested replay end. No bars after the replay request are ever included.
    """

    completed = completed_candles(minute_bars)
    requested_end = trade.exit_time + timedelta(minutes=15) if trade.exit_time else request.end_utc
    window_start = max(request.start_utc, trade.intent_time - timedelta(minutes=60))
    window_end = min(request.end_utc, requested_end)
    one_minute = [
        bar for bar in completed if window_start <= bar.timestamp < window_end
    ]

    fifteen_all = aggregate_15m(completed)
    fifteen = [
        bar for bar in fifteen_all if window_start <= bar.timestamp < window_end
    ]

    markers = [
        ReplayChartMarker(
            trade.intent_time,
            "intent",
            "Intent",
            trade.ep_price,
        ),
        ReplayChartMarker(
            trade.s1_time,
            "s1",
            "S1",
            _price_at(completed, trade.s1_time, None),
        ),
        ReplayChartMarker(
            trade.aoi_time,
            "aoi",
            "AOI touch",
            _price_at(completed, trade.aoi_time, None),
        ),
        ReplayChartMarker(
            trade.s2_time,
            "s2",
            "S2",
            _price_at(completed, trade.s2_time, None),
        ),
        ReplayChartMarker(trade.entry_time, "x", "X / entry", trade.entry_price),
    ]
    for event in trade.lifecycle:
        if event.event == "ENTRY":
            continue
        markers.append(
            ReplayChartMarker(
                event.timestamp,
                event.event.lower(),
                event.event.replace("_", " "),
                event.price,
            )
        )

    levels = [
        ReplayChartLevel("EX / invalidation", trade.ex_price or trade.stop_price, "invalidation"),
        ReplayChartLevel("PX", trade.px_price or trade.stop_price, "internal"),
        ReplayChartLevel("EP", trade.ep_price or trade.entry_price, "expansion"),
        ReplayChartLevel("Target", trade.target_price, "target"),
    ]

    return {
        "trade_id": trade.trade_id,
        "execution_tag": trade.execution_tag,
        "trigger_proxy": trade.trigger_proxy,
        "window_start_utc": window_start.isoformat().replace("+00:00", "Z"),
        "window_end_utc": window_end.isoformat().replace("+00:00", "Z"),
        "candles": {
            "1m": [_chart_bar(bar, "1m").to_dict() for bar in one_minute],
            "15m": [_chart_bar(bar, "15m").to_dict() for bar in fifteen],
        },
        "markers": [marker.to_dict() for marker in markers],
        "levels": [level.to_dict() for level in levels],
        "aoi": _aoi(trade),
    }
