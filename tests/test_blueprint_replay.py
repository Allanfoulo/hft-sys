from datetime import date

import pytest

from jevloop.blueprint_replay import (
    BLUEPRINT_VX_TAG,
    BlueprintRunStore,
    build_fixture_run,
    canonical_model_tag,
    public_model_tag,
)


def test_blueprint_vx_aliases_to_london_sweep_and_tags_run_and_rows():
    assert canonical_model_tag("blueprint-VX") == "london-sweep-v1"
    assert canonical_model_tag("london-sweep-v1") == "london-sweep-v1"
    assert public_model_tag("london-sweep-v1") == BLUEPRINT_VX_TAG
    run = build_fixture_run(date(2026, 9, 23), date(2026, 9, 23))
    assert run.result["execution_model_tag"] == BLUEPRINT_VX_TAG
    assert run.result["run_id"] == run.run_id
    assert run.result["rows"][0]["execution_model_tag"] == BLUEPRINT_VX_TAG
    assert run.result["rows"][0]["trade_id"] in run.charts


def test_fixture_chart_contains_bars_levels_markers_and_timeframes():
    run = build_fixture_run(date(2026, 9, 23), date(2026, 9, 23))
    trade_id = next(iter(run.charts))
    one_minute = run.chart(trade_id, "1m")
    context = run.chart(trade_id, "15m")
    assert one_minute["proxy_notice"]
    assert one_minute["bars"]
    assert context["bars"]
    assert {level["kind"] for level in one_minute["levels"]} == {"invalidation", "internal", "expansion", "target"}
    assert {marker["kind"] for marker in one_minute["markers"]} >= {"intent", "s1", "aoi", "s2", "x", "target"}


def test_chart_rejects_unknown_trade_or_timeframe():
    run = build_fixture_run(date(2026, 9, 23), date(2026, 9, 23))
    trade_id = next(iter(run.charts))
    with pytest.raises(ValueError, match="timeframe"):
        run.chart(trade_id, "5s")
    with pytest.raises(KeyError, match="trade_id"):
        run.chart("missing", "1m")


def test_store_keeps_runs_isolated():
    store = BlueprintRunStore()
    first = store.put(build_fixture_run(date(2026, 9, 23), date(2026, 9, 23)))
    second = store.put(build_fixture_run(date(2026, 9, 24), date(2026, 9, 24)))
    assert store.get(first.run_id) is first
    assert store.get(second.run_id) is second
    assert first.run_id != second.run_id
