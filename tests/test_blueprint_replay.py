from datetime import date
import json
import socketserver
import threading
import time
from urllib.request import Request, urlopen

import pytest

from jevloop.blueprint_replay import (
    BLUEPRINT_VX_TAG,
    BlueprintRunStore,
    build_fixture_run,
    canonical_model_tag,
    public_model_tag,
)
from jevloop.serve import Handler


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture()
def replay_server():
    server = _ThreadingServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _json_request(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=body, method=method, headers={"Content-Type": "application/json"} if body else {})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except Exception as exc:
        if hasattr(exc, "read"):
            return exc.code, json.loads(exc.read())
        raise


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
    assert one_minute["chart"]["candles"]["1m"]
    assert context["chart"]["candles"]["15m"]
    assert {level["role"] for level in one_minute["chart"]["levels"]} == {"invalidation", "internal", "expansion", "target"}
    assert {marker["kind"] for marker in one_minute["chart"]["markers"]} >= {"intent", "s1", "aoi", "s2", "x", "break_even", "profit_lock", "target"}


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


def test_background_job_and_lazy_chart_routes(replay_server):
    status, body = _json_request(
        replay_server + "/api/replay/jobs",
        "POST",
        {
            "source": "fixture",
            "execution_model_tag": "blueprint-VX",
            "start_utc": "2026-09-23T00:00:00Z",
            "end_utc": "2026-09-23T23:59:59Z",
        },
    )
    assert status == 202
    job_id = body["job"]["job_id"]
    for _ in range(50):
        status, job_body = _json_request(replay_server + f"/api/replay/jobs/{job_id}")
        assert status == 200
        if job_body["job"]["state"] == "complete":
            break
        time.sleep(0.02)
    assert job_body["job"]["state"] == "complete"
    result = job_body["job"]["result"]
    row = result["rows"][0]
    status, chart = _json_request(
        replay_server + f"/api/replay/{result['run_id']}/trades/{row['trade_id']}/chart?timeframe=15m"
    )
    assert status == 200
    assert chart["chart"]["selected_timeframe"] == "15m"
    assert chart["chart"]["candles"]["15m"]
    assert chart["chart"]["markers"]


def test_chart_route_rejects_unknown_run_and_timeframe(replay_server):
    status, body = _json_request(replay_server + "/api/replay/missing/trades/missing/chart?timeframe=1m")
    assert status == 404
    assert body["ok"] is False
