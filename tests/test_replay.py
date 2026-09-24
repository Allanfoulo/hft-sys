from datetime import datetime, timedelta, timezone
import time

import pytest

from jevloop.isx.models import Candle
from jevloop.replay.data import AlpacaMinuteBarsProvider, FixtureMinuteBarsProvider
from jevloop.replay.engine import ReplayEngine, aggregate_15m
from jevloop.replay.models import (
    ReplayLifecycleEvent,
    ReplayRequest,
    ReplayResultKind,
    ReplaySide,
    ReplayTrade,
)
from jevloop.replay.service import (
    ReplayNotFoundError,
    ReplayProviderError,
    ReplayJobManager,
    ReplayService,
    ReplayValidationError,
)


UTC = timezone.utc
DAY = datetime(2026, 9, 24, tzinfo=UTC)


def request(**overrides):
    payload = {
        "source": "fixture",
        "symbol": "BTC/USD",
        "start_utc": "2026-09-24T00:00:00Z",
        "end_utc": "2026-09-25T00:00:00Z",
    }
    payload.update(overrides)
    return ReplayRequest.from_payload(payload)


def test_replay_request_requires_utc_and_uses_isx_lifecycle_defaults():
    parsed = request()
    assert parsed.target_r == 4
    assert parsed.breakeven_r == 1
    assert parsed.profit_lock_trigger_r == 2
    assert parsed.profit_lock_r == 1
    with pytest.raises(ValueError, match="UTC"):
        request(start_utc="2026-09-24T00:00:00")
    with pytest.raises(ValueError, match="after"):
        request(end_utc="2026-09-23T00:00:00Z")


def test_fixture_replay_is_deterministic_and_tagged():
    service = ReplayService()
    first = service.run(request().to_dict())
    second = service.run(request().to_dict())
    assert first.to_dict() == second.to_dict()
    assert first.summary.trades == 1
    trade = first.trades[0]
    assert trade.execution_tag.startswith("ISX-REPLAY-BTCUSD-")
    assert trade.trigger_proxy == "1m-trigger-proxy"
    assert trade.result is ReplayResultKind.TARGET
    assert trade.r_multiple == pytest.approx(4.0)
    assert [event.event for event in trade.lifecycle] == [
        "ENTRY",
        "BREAK_EVEN",
        "PROFIT_LOCK",
        "TARGET",
    ]
    assert first.run_id == second.run_id
    assert trade.ex_price is not None
    assert trade.px_price is not None
    assert trade.ep_price is not None


def test_replay_reports_progress_stages_and_completion():
    events = []
    ReplayService().run(request().to_dict(), progress=events.append)

    assert events[0]["stage"] == "validating"
    assert any(event["stage"] == "fetching" for event in events)
    replay_events = [event for event in events if event["stage"] == "replaying"]
    assert replay_events
    assert replay_events[-1]["processed"] == replay_events[-1]["total"]
    assert events[-1]["stage"] == "complete"


def test_replay_job_manager_reports_background_completion():
    manager = ReplayJobManager(ReplayService())
    started = manager.start(request().to_dict())
    deadline = time.monotonic() + 2
    status = manager.status(started["job_id"])
    while status and status["status"] not in {"complete", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
        status = manager.status(started["job_id"])

    assert status is not None
    assert status["status"] == "complete"
    assert status["progress"] == 1.0
    assert status["result"]["run_id"]


def test_trade_chart_reuses_completed_ohlc_and_marks_isx_sequence():
    service = ReplayService()
    result = service.run(request().to_dict())
    trade = result.trades[0]
    chart = service.trade_chart(result.run_id, trade.trade_id)

    assert chart["candles"]["1m"]
    assert chart["candles"]["15m"]
    assert chart["window_start_utc"].endswith("Z")
    assert chart["window_end_utc"].endswith("Z")
    assert [marker["kind"] for marker in chart["markers"][:5]] == [
        "intent",
        "s1",
        "aoi",
        "s2",
        "x",
    ]
    assert {level["role"] for level in chart["levels"]} == {
        "invalidation",
        "internal",
        "expansion",
        "target",
    }
    assert chart["aoi"]["fib_low"] == pytest.approx(0.618)
    assert chart["aoi"]["fib_high"] == pytest.approx(0.790)


def test_trade_chart_is_lazy_and_unknown_runs_are_rejected():
    service = ReplayService()
    result = service.run(request().to_dict())
    assert result.to_dict()["run_id"] == result.run_id
    with pytest.raises(ReplayNotFoundError, match="expired"):
        service.trade_chart("missing-run", result.trades[0].trade_id)


def test_replay_can_return_open_outcomes_at_range_end():
    result = ReplayService().run(
        request(end_utc="2026-09-24T01:42:00Z").to_dict()
    )
    assert result.summary.open_trades == 1
    assert result.trades[0].result is ReplayResultKind.OPEN
    assert result.trades[0].exit_time is None
    assert result.trades[0].pnl_usd is None


def test_fixture_range_produces_one_tagged_trade_per_session():
    result = ReplayService().run(
        request(end_utc="2026-09-26T00:00:00Z").to_dict()
    )
    assert result.summary.sessions == 2
    assert result.summary.trades == 2
    assert len({trade.execution_tag for trade in result.trades}) == 2


def test_completed_15m_aggregation_rejects_incomplete_bucket_and_gaps():
    bars = [
        Candle(DAY + timedelta(minutes=i), 10, 11, 9, 10)
        for i in range(14)
    ]
    assert aggregate_15m(bars) == []
    bars.append(Candle(DAY + timedelta(minutes=16), 10, 11, 9, 10))
    assert aggregate_15m(bars) == []


def test_alpaca_provider_paginates_without_order_capability():
    class ReadOnlyClient:
        def __init__(self):
            self.calls = []

        def get_historical_minute_bars(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return [Candle(DAY, 10, 11, 9, 10)], "next"
            return [Candle(DAY + timedelta(minutes=1), 10, 11, 9, 10)], None

        def submit_market_order(self, *args, **kwargs):
            raise AssertionError("replay must never submit orders")

    client = ReadOnlyClient()
    provider = AlpacaMinuteBarsProvider(client)
    bars = provider.get_bars("BTC/USD", DAY, DAY + timedelta(minutes=2))
    assert len(bars) == 2
    assert client.calls[1]["page_token"] == "next"


def test_alpaca_source_path_is_read_only():
    fixture = FixtureMinuteBarsProvider()

    class ReadOnlyAlpaca:
        def get_historical_minute_bars(self, **kwargs):
            bars = fixture.get_bars("BTC/USD", kwargs["start_utc"], kwargs["end_utc"])
            return bars, None

        def submit_market_order(self, *args, **kwargs):
            raise AssertionError("replay must never submit orders")

    service = ReplayService(client_factory=lambda **kwargs: ReadOnlyAlpaca())
    result = service.run(request(source="alpaca").to_dict())
    assert result.summary.trades == 1


def test_service_rejects_equities_and_empty_providers():
    with pytest.raises(ReplayValidationError, match="crypto"):
        ReplayService().run(request(symbol="AAPL").to_dict())

    class EmptyProvider:
        def get_bars(self, symbol, start_utc, end_utc):
            return []

    with pytest.raises(ReplayProviderError, match="no completed"):
        ReplayService(fixture_provider=EmptyProvider()).run(request().to_dict())


def test_engine_has_no_order_side_effect_dependency():
    bars = FixtureMinuteBarsProvider().get_bars("BTC/USD", DAY, DAY + timedelta(days=1))
    result = ReplayEngine().replay(bars, request())
    assert result.summary.trades == 1


def test_same_bar_stop_and_target_resolves_stop_first():
    req = request()
    trade = ReplayTrade(
        trade_id="same-bar",
        date_utc="2026-09-24",
        symbol="BTC/USD",
        side=ReplaySide.BUY,
        setup_id="ISX-B-test",
        execution_tag="ISX-REPLAY-BTCUSD-test-0001",
        trigger_proxy="1m-trigger-proxy",
        intent_time=DAY,
        s1_time=DAY,
        aoi_time=DAY,
        s2_time=DAY,
        entry_time=DAY,
        entry_price=10,
        stop_price=9,
        target_price=14,
        active_stop=9,
    )
    trade.lifecycle.append(ReplayLifecycleEvent(DAY, "ENTRY", 10, 9, 14))
    closed = ReplayEngine()._advance_trade(trade, Candle(DAY + timedelta(minutes=1), 10, 14, 8, 10), req)
    assert closed is True
    assert trade.result is ReplayResultKind.STOP
    assert trade.r_multiple == -1
