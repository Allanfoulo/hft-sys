from datetime import date

import pytest

from jevloop.replay import replay_range, run_demo


def test_offline_replay_runs_outside_the_wall_clock(capsys):
    assert run_demo(date(2026, 9, 23), "london-sweep-test") == 0
    output = capsys.readouterr().out
    assert "execution_model_tag: london-sweep-test" in output
    assert "replay passed" in output


def test_replay_range_returns_tagged_rows_and_summary():
    result = replay_range(date(2026, 9, 23), date(2026, 9, 25), "range-test")
    assert result["fixture"] is True
    assert result["summary"]["sessions"] == 3
    assert result["summary"]["trades"] == 3
    assert result["summary"]["total_r"] == pytest.approx(9.0)
    assert all(row["execution_model_tag"] == "range-test" for row in result["rows"])


def test_fixture_replay_exercises_profit_and_loss_paths():
    result = replay_range(date(2026, 8, 1), date(2026, 8, 3), "mixed-test")
    assert result["summary"]["wins"] == 2
    assert result["summary"]["losses"] == 1
    assert result["summary"]["total_r"] == pytest.approx(5.0)
    assert {row["profit_loss"] for row in result["rows"]} == {"Profit", "Loss"}
    assert all(row["simulated"] is True for row in result["rows"])


def test_replay_range_rejects_reversed_or_oversized_ranges():
    with pytest.raises(ValueError, match="on or after"):
        replay_range(date(2026, 9, 24), date(2026, 9, 23))
    with pytest.raises(ValueError, match="91 days"):
        replay_range(date(2026, 1, 1), date(2026, 4, 2))
