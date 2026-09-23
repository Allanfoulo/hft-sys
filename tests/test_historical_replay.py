from datetime import datetime, timezone

import pytest

from jevloop.historical_replay import (
    HistoricalReplayError,
    _aggregate,
    _fetch_minute_bars,
    _utc_datetime,
    replay_historical_range,
)
from jevloop.market_structure import Bar


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def test_fetch_minute_bars_uses_auth_and_follows_page_tokens():
    calls = []

    def request_get(url, *, headers, params, timeout):
        calls.append((url, headers, params.copy(), timeout))
        if len(calls) == 1:
            return FakeResponse(
                {
                    "bars": {"BTC/USD": [{"t": "2026-09-23T08:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 4}]},
                    "next_page_token": "next",
                }
            )
        return FakeResponse(
            {"bars": {"BTC/USD": [{"t": "2026-09-23T08:01:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101, "v": 5}]}}
        )

    bars = _fetch_minute_bars(
        "BTC/USD",
        _utc_datetime(datetime(2026, 9, 23, tzinfo=timezone.utc).date(), False),
        _utc_datetime(datetime(2026, 9, 23, tzinfo=timezone.utc).date(), True),
        "key",
        "secret",
        request_get=request_get,
    )

    assert [bar.close for bar in bars] == [100.5, 101.0]
    assert calls[0][0].endswith("/bars")
    assert calls[0][1] == {"APCA-API-KEY-ID": "key", "APCA-API-SECRET-KEY": "secret"}
    assert calls[0][2]["timeframe"] == "1Min"
    assert calls[1][2]["page_token"] == "next"


def test_fetch_minute_bars_surfaces_alpaca_errors():
    def request_get(*args, **kwargs):
        return FakeResponse({}, status_code=429, text="rate limited")

    with pytest.raises(HistoricalReplayError, match="HTTP 429"):
        _fetch_minute_bars(
            "BTC/USD",
            _utc_datetime(datetime(2026, 9, 23, tzinfo=timezone.utc).date()),
            _utc_datetime(datetime(2026, 9, 23, tzinfo=timezone.utc).date(), True),
            "key",
            "secret",
            request_get=request_get,
        )


def test_fetch_minute_bars_surfaces_network_errors():
    import requests

    def request_get(*args, **kwargs):
        raise requests.ConnectionError("network unavailable")

    with pytest.raises(HistoricalReplayError, match="request failed"):
        _fetch_minute_bars(
            "BTC/USD",
            _utc_datetime(datetime(2026, 9, 23, tzinfo=timezone.utc).date()),
            _utc_datetime(datetime(2026, 9, 23, tzinfo=timezone.utc).date(), True),
            "key",
            "secret",
            request_get=request_get,
        )


def test_aggregate_builds_fixed_15_minute_ohlcv_bars():
    start = _utc_datetime(datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc).date(), False).timestamp()
    bars = [
        Bar(start, 60, 100, 101, 99, 100.5, 4),
        Bar(start + 60, 60, 100.5, 102, 100, 101, 5),
    ]
    aggregate = _aggregate(bars, 900)
    assert len(aggregate) == 1
    assert aggregate[0].open == 100
    assert aggregate[0].high == 102
    assert aggregate[0].low == 99
    assert aggregate[0].close == 101
    assert aggregate[0].volume == 9


def test_historical_replay_requires_credentials(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(HistoricalReplayError, match="ALPACA_API_KEY"):
        replay_historical_range(
            datetime(2026, 9, 23, tzinfo=timezone.utc).date(),
            datetime(2026, 9, 23, tzinfo=timezone.utc).date(),
        )
