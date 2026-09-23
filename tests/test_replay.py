from datetime import date

from jevloop.replay import run_demo


def test_offline_replay_runs_outside_the_wall_clock(capsys):
    assert run_demo(date(2026, 9, 23), "london-sweep-test") == 0
    output = capsys.readouterr().out
    assert "execution_model_tag: london-sweep-test" in output
    assert "replay passed" in output
