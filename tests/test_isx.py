from datetime import datetime, timedelta, timezone

import pytest

from jevloop.loop import run
from jevloop.isx.engine import ISXEngine
from jevloop.isx.models import Candle, Direction, ISXPhase, PivotKind
from jevloop.isx.structure import detect_breaks, detect_pivots
from jevloop.limits import Limits
from jevloop.market_data import AlpacaHistoricalBarsProvider
from jevloop.policy import Action, ISX_X
from jevloop.risk import check as risk_check


UTC = timezone.utc


def c(i, high, low, close, *, start=datetime(2026, 1, 1, tzinfo=UTC), complete=True):
    return Candle(start + timedelta(minutes=15 * i), (high + low) / 2, high, low, close, complete)


def bullish_structure(start):
    values = [(11, 9, 10), (13, 10, 12), (11, 8, 9), (14, 10, 13), (12, 9, 11), (15, 10, 14)]
    return [Candle(start + timedelta(hours=i), (h + l) / 2, h, l, close) for i, (h, l, close) in enumerate(values)]


def bearish_structure(start):
    values = [(11, 9, 10), (10, 7, 8), (12, 8, 11), (9, 6, 7), (11, 7, 10), (8, 5, 6)]
    return [Candle(start + timedelta(hours=i), (h + l) / 2, h, l, close) for i, (h, l, close) in enumerate(values)]


def m15_sequence(start):
    values = [(10, 8, 9), (12, 9, 11), (10, 7, 8), (11, 8, 9), (9, 6, 7), (11, 9, 10), (12, 9, 11), (11, 8, 10), (13, 9, 12)]
    return [Candle(start + timedelta(minutes=15 * i), (h + l) / 2, h, l, close) for i, (h, l, close) in enumerate(values)]


def test_wick_breach_without_close_is_not_a_break():
    bars = [c(0, 9, 7, 8), c(1, 12, 8, 10), c(2, 10, 7, 9), c(3, 13, 8, 9)]
    assert not [b for b in detect_breaks(bars, detect_pivots(bars, 1, 1)) if b.direction is Direction.BULLISH]


def test_close_beyond_pivot_close_is_bos_and_against_direction_is_choch():
    bars = [c(0, 9, 7, 8), c(1, 12, 8, 10), c(2, 10, 7, 9), c(3, 13, 8, 11)]
    breaks = detect_breaks(bars, detect_pivots(bars, 1, 1), Direction.BULLISH)
    assert breaks[-1].direction is Direction.BULLISH
    assert breaks[-1].kind.value == "BOS"

    bearish = [c(0, 12, 9, 11), c(1, 10, 8, 9), c(2, 11, 9, 10), c(3, 8, 7, 8)]
    breaks = detect_breaks(bearish, detect_pivots(bearish, 1, 1), Direction.BULLISH)
    assert any(b.direction is Direction.BEARISH and b.kind.value == "CHOCH" for b in breaks)


def test_incomplete_candles_are_ignored():
    bars = [c(0, 9, 7, 8), c(1, 12, 8, 10), c(2, 10, 7, 9), c(3, 13, 8, 11, complete=False)]
    assert all(p.timestamp != bars[3].timestamp for p in detect_pivots(bars, 1, 1))


def test_pivots_are_classified_from_confirmed_wick_extremes():
    bars = [
        c(0, 10, 8, 9),
        c(1, 13, 9, 12),
        c(2, 11, 7, 8),
        c(3, 14, 10, 13),
        c(4, 12, 9, 11),
    ]
    pivots = detect_pivots(bars, 1, 1)
    assert any(p.side == "high" and p.kind is PivotKind.HH for p in pivots)
    assert any(p.side == "low" and p.kind is PivotKind.LL for p in pivots)


def test_correct_isx_sequence_emits_one_tagged_x_and_is_idempotent():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    h = bullish_structure(start)
    m = m15_sequence(start + timedelta(days=1))
    engine = ISXEngine(pivot_left=1, pivot_right=1)
    decision = engine.update({"H4": h, "H1": h, "M15": m})
    assert decision.x_ready is True
    assert decision.action == ISX_X
    assert decision.execution_tag.startswith("ISX-X:")
    again = engine.update({"H4": h, "H1": h, "M15": m})
    assert again.x_ready is False
    assert again.action == "STAND_DOWN"
    assert engine.state.phase is ISXPhase.EXECUTED


def test_h4_h1_disagreement_stands_down():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bullish = bullish_structure(start)
    bearish = bearish_structure(start)
    decision = ISXEngine(pivot_left=1, pivot_right=1).update({"H4": bullish, "H1": bearish, "M15": m15_sequence(start + timedelta(days=1))})
    assert decision.action == "STAND_DOWN"
    assert "neutral" in decision.reason or "disagreement" in decision.reason


def test_s1_cannot_occur_without_a_confirmed_intent():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    neutral = [c(i, 10, 8, 9, start=start) for i in range(6)]
    decision = ISXEngine(pivot_left=1, pivot_right=1).update(
        {"H4": neutral, "H1": neutral, "M15": m15_sequence(start + timedelta(days=1))}
    )
    assert decision.action == "STAND_DOWN"
    assert decision.observability["s1_time"] is None
    assert decision.observability["intent"] == Direction.NEUTRAL.value


def test_aoi_touch_alone_is_not_x_and_s2_before_aoi_is_rejected():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    h = bullish_structure(start)
    m = m15_sequence(start + timedelta(days=1))
    no_aoi = m[:5]
    decision = ISXEngine(pivot_left=1, pivot_right=1).update({"H4": h, "H1": h, "M15": no_aoi})
    assert decision.action == "STAND_DOWN"
    assert decision.x_ready is False

    # An aligned break before the later AOI touch is not S2.
    s2_before_aoi = m[:5] + [m[5], m[6], m[7], c(8, 11, 9, 10, start=m[0].timestamp + timedelta(days=0))]
    decision = ISXEngine(pivot_left=1, pivot_right=1).update({"H4": h, "H1": h, "M15": s2_before_aoi})
    assert decision.action == "STAND_DOWN"
    assert decision.x_ready is False


def test_79_percent_retracement_remains_valid():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    h = bullish_structure(start)
    engine = ISXEngine(pivot_left=1, pivot_right=1)
    m = m15_sequence(start + timedelta(days=1))[:-1]
    # The engine's H1 range is EX=8, EP=14, so close 9.26 is exactly 79%.
    m.append(Candle(m[-1].timestamp + timedelta(minutes=15), 10, 9, 9.26, 9.26))
    decision = engine.update({"H4": h, "H1": h, "M15": m})
    assert engine.state.aoi_qualified is True
    assert decision.x_ready is False


def test_ex_invalidation_cancels_setup():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    h = bullish_structure(start)
    m = m15_sequence(start + timedelta(days=1))[:5]
    m.append(Candle(m[-1].timestamp + timedelta(minutes=15), 9, 7, 7.9, 7.9))
    decision = ISXEngine(pivot_left=1, pivot_right=1).update({"H4": h, "H1": h, "M15": m})
    assert decision.action == "STAND_DOWN"
    assert decision.phase is ISXPhase.INVALIDATED


def test_replay_in_fresh_engine_is_deterministic():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = {"H4": bullish_structure(start), "H1": bullish_structure(start), "M15": m15_sequence(start + timedelta(days=1))}
    a = ISXEngine(pivot_left=1, pivot_right=1).update(bars)
    b = ISXEngine(pivot_left=1, pivot_right=1).update(bars)
    assert a.observability == b.observability
    assert a.execution_tag == b.execution_tag


def test_isx_x_still_runs_through_hard_risk_veto():
    verdict = risk_check(
        {"drawdown_pct": 0.20, "daily_loss_usd": 0, "position_age_s": 0, "data_age_s": 0, "leverage": 1},
        order_notional_usd=20,
        limits=Limits(),
        api_error_streak=0,
        decision_latency_ms=100,
    )
    assert verdict.ok is False and verdict.kill is True


def test_isx_mode_refuses_live_execution():
    assert run("BTC/USD", ticks=1, mock=True, limits=Limits(), live=True, isx=True) == 1


def test_historical_provider_keeps_vendor_boundary_out_of_isx_engine():
    class FakeClient:
        def get_historical_bars(self, timeframe, *, limit, end):
            return (timeframe, limit, end)

    result = AlpacaHistoricalBarsProvider(FakeClient()).get_bars("M15", limit=12)
    assert result == ("M15", 12, None)
