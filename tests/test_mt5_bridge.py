from datetime import datetime, timedelta, timezone

import pytest

from jevloop.isx.models import Candle
from jevloop.mt5_bridge import MT5BridgeService, MT5BridgeValidationError


UTC = timezone.utc


def bullish_structure(start):
    values = [(11, 9, 10), (13, 10, 12), (11, 8, 9), (14, 10, 13), (12, 9, 11), (15, 10, 14)]
    return [Candle(start + timedelta(hours=i), (h + l) / 2, h, l, close) for i, (h, l, close) in enumerate(values)]


def m15_sequence(start):
    values = [(10, 8, 9), (12, 9, 11), (10, 7, 8), (11, 8, 9), (9, 6, 7), (11, 9, 10), (12, 9, 11), (11, 8, 10), (13, 9, 12)]
    return [Candle(start + timedelta(minutes=15 * i), (h + l) / 2, h, l, close) for i, (h, l, close) in enumerate(values)]


def bars_payload(series):
    return [
        {
            "timestamp_utc": candle.timestamp.isoformat().replace("+00:00", "Z"),
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "complete": candle.complete,
        }
        for candle in series
    ]


def payload(**overrides):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    h = bullish_structure(start)
    data = {
        "session_id": "demo-account:BTCUSD",
        "symbol": "BTCUSD",
        "bars": {
            "H4": bars_payload(h),
            "H1": bars_payload(h),
            "M15": bars_payload(m15_sequence(start + timedelta(days=1))),
        },
        "trigger": {"time": start.timestamp(), "bid": 14.9, "ask": 15.0},
    }
    data.update(overrides)
    return data


def test_bridge_rejects_missing_timeframe():
    data = payload()
    del data["bars"]["M15"]
    with pytest.raises(MT5BridgeValidationError, match="bars.M15"):
        MT5BridgeService().decide(data)


def test_bridge_reuses_python_isx_engine_and_deduplicates_x():
    service = MT5BridgeService(pivot_left=1, pivot_right=1)
    first = service.decide(payload())

    assert first["model"] == "isx-python-hybrid-v1"
    assert first["live_execution_allowed"] is False
    assert first["decision"]["action"] == "ISX_X"
    assert first["decision"]["execution_tag"].startswith("ISX-X:")
    assert first["execution"] == {
        "allowed": True,
        "side": "BUY",
        "entry": 15.0,
        "stop": 8.0,
        "target": 43.0,
        "target_r": 4.0,
        "trigger_proxy": "mt5-bid-ask-at-poll",
    }

    second = service.decide(payload())
    assert second["decision"]["action"] == "STAND_DOWN"
    assert second["decision"]["reason"] == "duplicate closed-candle set"
    assert second["execution"]["allowed"] is False
    assert service.status()["sessions"][0]["requests"] == 2


def test_bridge_rejects_inconsistent_ohlc_and_invalid_target():
    data = payload()
    data["bars"]["H4"][0]["low"] = 12
    with pytest.raises(MT5BridgeValidationError, match="inconsistent OHLC"):
        MT5BridgeService().decide(data)

    with pytest.raises(MT5BridgeValidationError, match="target_r"):
        MT5BridgeService().decide(payload(target_r=0))
