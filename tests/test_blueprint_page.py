from pathlib import Path


PAGE = Path(__file__).parents[1] / "dashboard" / "blueprint-vx-replay.html"


def test_blueprint_page_contains_reference_sections_and_contract():
    html = PAGE.read_text(encoding="utf-8")
    for required in (
        'id="summary"',
        'id="analytics-grid"',
        'id="chart"',
        'id="trade-chart-svg"',
        'id="ledger"',
        'id="detail"',
        'id="run-toast"',
        'option value="1m"',
        'option value="15m"',
        "blueprint-VX",
        "1m trigger proxy",
        "/api/replay/jobs",
        "/api/replay/",
    ):
        assert required in html
    assert "submit_market_order" not in html
    assert "submit_limit_order" not in html


def test_legacy_staging_replay_page_is_not_modified_by_new_page():
    replay = (PAGE.parent / "replay.html").read_text(encoding="utf-8")
    assert "London sweep v1" in replay
    assert "blueprint-VX" not in replay
