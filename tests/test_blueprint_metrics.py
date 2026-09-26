import pytest

from jevloop.blueprint_metrics import calculate_metrics


def test_metrics_distinguish_losses_and_break_even_and_calculate_rates():
    rows = [
        {"date": "2026-09-01", "r_multiple": 3.0, "pnl_usd": 30, "outcome": "target", "transitions": ["break_even", "target"]},
        {"date": "2026-09-01", "r_multiple": 0.0, "pnl_usd": 0, "outcome": "stop", "transitions": ["break_even", "stop"]},
        {"date": "2026-09-02", "r_multiple": -1.0, "pnl_usd": -10, "outcome": "stop", "transitions": ["stop"]},
    ]
    metrics = calculate_metrics(rows, risk_usd=10)
    assert metrics["wins"] == 1
    assert metrics["losses"] == 1
    assert metrics["breakevens"] == 1
    assert metrics["win_rate"] == pytest.approx(1 / 3)
    assert metrics["loss_rate"] == pytest.approx(1 / 3)
    assert metrics["breakeven_rate"] == pytest.approx(1 / 3)
    assert metrics["expectancy_r"] == pytest.approx(2 / 3)
    assert metrics["profit_factor"] == pytest.approx(3.0)
    assert metrics["avg_win_r"] == pytest.approx(3.0)
    assert metrics["avg_loss_r"] == pytest.approx(-1.0)


def test_metrics_cover_streaks_drawdown_lifecycle_and_daily_maxima():
    rows = [
        {"date": "2026-09-01", "r_multiple": -1.0, "pnl_usd": -10, "outcome": "stop", "transitions": ["profit_lock", "stop"]},
        {"date": "2026-09-01", "r_multiple": 0.0, "pnl_usd": 0, "outcome": "stop", "transitions": ["break_even", "stop"]},
        {"date": "2026-09-01", "r_multiple": 3.0, "pnl_usd": 30, "outcome": "target", "transitions": ["break_even", "profit_lock", "target"]},
    ]
    metrics = calculate_metrics(rows, risk_usd=10)
    assert metrics["max_non_positive_streak"] == 2
    assert metrics["max_drawdown_r"] == pytest.approx(1.0)
    assert metrics["max_drawdown_usd"] == pytest.approx(10.0)
    assert metrics["target_exits"] == 1
    assert metrics["stop_losses"] == 1
    assert metrics["stop_breakevens"] == 1
    assert metrics["stop_profit_locks"] == 0
    assert metrics["break_even_moves"] == 2
    assert metrics["profit_lock_moves"] == 2
    assert metrics["max_trades_per_day"] == 3
    assert metrics["max_losses_per_day"] == 1


def test_metrics_empty_input_has_safe_zero_values():
    metrics = calculate_metrics([], risk_usd=10)
    assert metrics["profit_factor"] is None
    assert metrics["expectancy_r"] == 0.0
    assert metrics["max_drawdown_r"] == 0.0
