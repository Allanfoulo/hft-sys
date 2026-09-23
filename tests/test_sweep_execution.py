from datetime import datetime, timezone

import pytest

from jevloop.execution.sweep import (
    ShortInventoryError,
    SweepPosition,
    SweepRiskConfig,
    TradePlanError,
    build_trade_plan,
)
from jevloop.market_structure import Bar
from jevloop.session_strategy import EntrySignal


def ts(second: int) -> float:
    return datetime(2026, 1, 5, 8, 0, second, tzinfo=timezone.utc).timestamp()


def long_signal() -> EntrySignal:
    return EntrySignal(1, "long", ts(0), 100.0, 99.0, 99.0)


def short_signal() -> EntrySignal:
    return EntrySignal(2, "short", ts(0), 100.0, 101.0, 101.0)


def test_trade_plan_caps_risk_and_targets_three_r():
    plan = build_trade_plan(long_signal())
    assert plan.quantity == pytest.approx(0.25)  # $5 risk / $1 stop, capped at $25 notional
    assert plan.max_loss_usd == pytest.approx(0.25)
    assert plan.target_price == pytest.approx(103.0)


def test_short_requires_existing_spot_inventory():
    with pytest.raises(ShortInventoryError):
        build_trade_plan(short_signal())
    plan = build_trade_plan(short_signal(), inventory_qty=0.1)
    assert plan.quantity == pytest.approx(0.1)


def test_invalid_stop_is_rejected():
    with pytest.raises(TradePlanError):
        build_trade_plan(EntrySignal(1, "long", ts(0), 100, 101, 101))


def test_position_moves_to_breakeven_then_profit_lock_then_target():
    plan = build_trade_plan(long_signal(), SweepRiskConfig(max_order_notional_usd=100))
    position = SweepPosition(plan)
    be = position.update(Bar(ts(1), 5, 100, 101.1, 100.1, 101))
    assert be.event == "breakeven"
    assert be.state.current_stop == pytest.approx(100)
    locked = position.update(Bar(ts(6), 5, 101, 102.6, 100.5, 102.5))
    assert locked.event == "profit_lock"
    assert locked.state.current_stop == pytest.approx(102.0)
    target = position.update(Bar(ts(11), 5, 102, 103.1, 102.1, 103))
    assert target.event == "target"
    assert target.state.status == "closed"
    assert target.state.exit_price == pytest.approx(103.0)


def test_stop_wins_when_a_bar_hits_stop_and_target():
    plan = build_trade_plan(long_signal(), SweepRiskConfig(max_order_notional_usd=100))
    position = SweepPosition(plan)
    transition = position.update(Bar(ts(1), 5, 100, 103, 98.5, 101))
    assert transition.event == "stop"
    assert transition.state.exit_price == pytest.approx(99.0)
