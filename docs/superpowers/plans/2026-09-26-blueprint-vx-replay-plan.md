# Blueprint-VX Candle-by-Candle Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, pixel-matched Blueprint-VX candle-by-candle replay page on `feature/london-sweep-execution`, backed by the existing London-sweep model and isolated from the legacy replay page.

**Architecture:** Keep the current London-sweep model as the single source of trade outcomes. Add a focused replay-run store that preserves the completed bars and lifecycle artifacts needed by the reference candle chart, expose read-only background-job and chart routes, and render a new page using the reference ISX Replay structure/styles with `blueprint-VX` as the public model tag.

**Tech Stack:** Python standard library `http.server`/threading, existing `jevloop` replay/model code, static HTML/CSS/JavaScript, pytest.

---

## File map

- Create `jevloop/blueprint_metrics.py`: pure analytics calculations for closed trades and lifecycle events.
- Create `jevloop/blueprint_replay.py`: Blueprint-VX aliasing, replay artifacts, stable run/trade IDs, in-memory job store, and chart payload construction.
- Modify `jevloop/historical_replay.py`: expose the fetched 1-minute bars and per-trade lifecycle/bar context to the artifact adapter without changing the existing `replay_historical_range()` response contract.
- Modify `jevloop/replay.py`: add fixture lifecycle/bar artifacts and the same analytics shape used by the historical path without changing existing CLI output.
- Modify `jevloop/serve.py`: add read-only Blueprint-VX job/status/chart routes and static page routing while preserving `/replay.json`.
- Create `dashboard/blueprint-vx-replay.html`: visual/interaction copy of `mql5-ea/dashboard/isx-replay.html` adapted to the Blueprint-VX endpoints and tag.
- Create `tests/test_blueprint_metrics.py`: deterministic analytics tests.
- Create `tests/test_blueprint_replay.py`: alias, run/trade IDs, bars, lifecycle, chart marker, and isolation tests.
- Modify `tests/test_historical_replay.py` only when a new public artifact hook needs coverage; retain all existing tests.
- Modify `tests/test_replay.py` only when fixture artifacts need coverage; retain all existing tests.

The unrelated working-tree edit in `jevloop/loop.py` remains untouched.

## Task 1: Add the pure analytics contract

**Files:**
- Create: `jevloop/blueprint_metrics.py`
- Create: `tests/test_blueprint_metrics.py`

- [ ] **Step 1: Write failing tests for the reference metrics.**

Use a small list of closed trade dictionaries with `r_multiple`, `pnl_usd`,
`date`, `outcome`, and `transitions`, then assert the exact fields consumed by
the reference page:

```python
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
    assert metrics["stop_profit_locks"] == 1
    assert metrics["break_even_moves"] == 2
    assert metrics["profit_lock_moves"] == 2
    assert metrics["max_trades_per_day"] == 3
    assert metrics["max_losses_per_day"] == 2

def test_metrics_empty_input_has_safe_zero_values():
    metrics = calculate_metrics([], risk_usd=10)
    assert metrics["profit_factor"] is None
    assert metrics["expectancy_r"] == 0.0
    assert metrics["max_drawdown_r"] == 0.0
```

- [ ] **Step 2: Run the focused test and verify it fails.**

Run `pytest -q tests/test_blueprint_metrics.py`. Expected: import failure for
`jevloop.blueprint_metrics`.

- [ ] **Step 3: Implement the pure calculation.**

Implement `calculate_metrics(rows: Sequence[Mapping[str, Any]], risk_usd: float) -> dict[str, Any]` in chronological order. Treat positive R as wins, negative R as actual losses, and exactly zero R as break-even. Use closed rows only, preserve `None` for undefined profit factor, calculate cumulative-R peak-to-trough drawdown, count lifecycle events by normalized lowercase names, and count daily rows/losses by ISO date.

- [ ] **Step 4: Run the focused tests.**

Run `pytest -q tests/test_blueprint_metrics.py`. Expected: all tests pass.

- [ ] **Step 5: Commit the analytics unit.**

Run `git add jevloop/blueprint_metrics.py tests/test_blueprint_metrics.py` and
`git commit -m "Add Blueprint-VX replay analytics"`.

## Task 2: Preserve replay artifacts and create chart payloads

**Files:**
- Create: `jevloop/blueprint_replay.py`
- Modify: `jevloop/historical_replay.py`
- Modify: `jevloop/replay.py`
- Create: `tests/test_blueprint_replay.py`

- [ ] **Step 1: Write failing artifact tests.**

Assert that a fixture run with tag `blueprint-VX` returns a stable `run_id`,
stable per-row `trade_id`, lifecycle records, completed 1-minute bars, and a
chart payload with the reference marker/level kinds and both timeframes. Also
assert that the alias is accepted while the underlying model remains the
London-sweep implementation.

- [ ] **Step 2: Run the focused test and verify it fails.**

Run `pytest -q tests/test_blueprint_replay.py`. Expected: missing module/API
failures.

- [ ] **Step 3: Add the artifact adapter.**

Expose `BLUEPRINT_VX_TAG = "blueprint-VX"` and
`LONDON_SWEEP_TAG = "london-sweep-v1"`. Implement:

```python
def canonical_model_tag(tag: str) -> str:
    if tag.strip().lower() in {"blueprint-vx", "london-sweep-v1"}:
        return "london-sweep-v1"
    raise ValueError("unsupported execution model tag")

def public_model_tag(tag: str) -> str:
    return "blueprint-VX" if canonical_model_tag(tag) == "london-sweep-v1" else tag.strip()

def build_chart_payload(run_id: str, trade_id: str, *, timeframe: str = "1m") -> dict[str, Any]:
    run = RUN_STORE.require(run_id)
    trade = run.trades[trade_id]
    bars = run.context_bars if timeframe == "15m" else run.trigger_bars[trade_id]
    return {
        "ok": True,
        "run_id": run_id,
        "trade_id": trade_id,
        "timeframe": timeframe,
        "proxy_notice": "1m trigger proxy",
        "bars": [bar_to_dict(bar) for bar in bars],
        "levels": trade.levels,
        "markers": trade.markers,
    }
```

The adapter must retain completed `Bar` objects for the requested trade and
aggregate them into 15-minute OHLC bars with the existing `_aggregate()`
logic. Marker kinds are `intent`, `s1`, `aoi`, `s2`, `x`, `break_even`,
`profit_lock`, `target`, and `stop`; levels are `invalidation`, `internal`,
`expansion`, and `target`.

- [ ] **Step 4: Add fixture artifacts without changing existing fixture output.**

Wrap the current `replay_range()` rows into a Blueprint-VX result that adds
artifacts and analytics while preserving the original function’s existing keys
and default `london-sweep-v1` behavior. Add lifecycle timestamps and OHLC bars
for the deterministic fixture path so chart tests never use network data.

- [ ] **Step 5: Add historical artifacts without fabricating tick data.**

Refactor the historical path internally so the fetched completed 1-minute bars
remain available to the adapter. Keep the current public historical response
shape and its explicit `resolution: "1m trigger proxy"` notice. Use the next
available completed minute as the trigger bar and aggregate 15-minute context;
never claim 5-second/tick fills.

- [ ] **Step 6: Run artifact tests.**

Run `pytest -q tests/test_blueprint_replay.py tests/test_replay.py tests/test_historical_replay.py`. Expected: all pass.

- [ ] **Step 7: Commit the artifact unit.**

Run `git add jevloop/blueprint_replay.py jevloop/historical_replay.py jevloop/replay.py tests/test_blueprint_replay.py` and `git commit -m "Add Blueprint-VX replay artifacts"`.

## Task 3: Add read-only background replay and chart routes

**Files:**
- Modify: `jevloop/serve.py`
- Modify: `tests/test_blueprint_replay.py`

- [ ] **Step 1: Write route tests.**

Exercise the handler with a temporary server/request helper and assert:

```text
POST /api/replay/jobs              -> 202 + job_id
GET  /api/replay/jobs/{job_id}    -> queued/running/complete or error
GET  /api/replay/{run_id}/trades/{trade_id}/chart?timeframe=1m
GET  /api/replay/{run_id}/trades/{trade_id}/chart?timeframe=15m
```

Assert invalid tags, invalid timeframe, unknown IDs, and replay failures return
JSON errors; assert no route calls any order-submission function.

- [ ] **Step 2: Implement the in-memory job store.**

Use a bounded `ThreadPoolExecutor` and a lock-protected dictionary keyed by
opaque UUID strings. Store only completed read-only replay results and chart
artifacts for the process lifetime; expose progress stage/message and a
heartbeat timestamp for the toast.

- [ ] **Step 3: Add the routes while preserving `/replay.json`.**

Parse JSON request bodies for `start`, `end`, `tag`, `source`, `symbol`, and
replay risk parameters. Canonicalize `blueprint-VX` to the London-sweep model,
return public tags as `blueprint-VX`, and set `Cache-Control: no-store`. Keep
the current synchronous `/replay.json` route and static file translation
unchanged for existing users.

- [ ] **Step 4: Run route tests.**

Run `pytest -q tests/test_blueprint_replay.py tests/test_replay.py`. Expected:
all pass.

- [ ] **Step 5: Commit the service unit.**

Run `git add jevloop/serve.py tests/test_blueprint_replay.py` and `git commit -m "Expose Blueprint-VX replay jobs and charts"`.

## Task 4: Build the pixel-matched Blueprint-VX page

**Files:**
- Create: `dashboard/blueprint-vx-replay.html`

- [ ] **Step 1: Copy the reference page structure and styles.**

Copy the reference page’s layout, CSS tokens, component order, responsive
breakpoints, and chart/detailed-panel DOM into the new page. Change only the
identity and endpoint values: page title/eyebrow to Blueprint-VX, default tag
to `blueprint-VX`, and fetch paths to the new read-only routes. Do not edit
`dashboard/isx-replay.html` or `dashboard/replay.html`.

- [ ] **Step 2: Wire the interaction contract.**

Keep the reference behavior: run button starts a job, toast polls status,
completed result renders summary/analytics/cumulative chart/ledger, row click
loads detail and calls the lazy chart endpoint, timeframe select redraws from
the returned 1-minute or 15-minute bars, and unavailable/error/empty states
remain visible and explanatory.

- [ ] **Step 3: Apply the exact chart annotations.**

Render green/red candlesticks and wicks, amber AOI band, red EX level, gray PX
level, blue EP level, green target, and marker colors/types matching the
reference legend. Keep the chart dimensions, axis labels, hover behavior, and
`1m trigger proxy` labels unchanged.

- [ ] **Step 4: Add a static smoke test.**

Assert the new page contains the required section IDs, both timeframe options,
the chart endpoint template, `blueprint-VX`, `1m trigger proxy`, and no order
submission URL. Assert the target branch has no diff for its existing
`dashboard/replay.html`; the reference `mql5-ea/dashboard/isx-replay.html`
remains outside the target checkout and is not edited by this feature.

## Task 5: End-to-end verification

**Files:**
- Modify: `tests/test_blueprint_replay.py` if integration fixtures need a small helper.

- [ ] **Step 1: Run all deterministic tests.**

Run `pytest -q`. Expected: the complete existing suite plus the new Blueprint-VX tests pass.

- [ ] **Step 2: Start the server on port 8767.**

Run from `D:\Development\hft_sys\jev-loop-staging`:

```text
C:\Python311\python.exe -c "from dotenv import load_dotenv; load_dotenv(r'C:\Users\cash crusaders\.claude\skills\jev-loop\.env'); from jevloop.serve import main; raise SystemExit(main(['--port','8767']))"
```

Verify `/index.html`, `/replay.html`, and `/blueprint-vx-replay.html` return
the expected page titles and that the new page can complete fixture and
historical runs without submitting orders.

- [ ] **Step 3: Perform browser comparison.**

Open the new page beside the reference `isx-replay.html` and compare desktop
and narrow layouts. Run a fixture replay, click multiple trades, switch both
timeframes, and force empty/invalid/unavailable states. Verify the ledger tag,
analytics, lifecycle detail, markers, levels, loading toast, and chart data.

- [ ] **Step 4: Review isolation and working tree.**

Run `git diff -- dashboard/isx-replay.html dashboard/replay.html` and verify no
legacy page changes. Confirm the only unrelated working-tree change remains
the pre-existing `jevloop/loop.py` edit.

- [ ] **Step 5: Commit the page and tests.**

Run `git add dashboard/blueprint-vx-replay.html tests/test_blueprint_replay.py` and
`git commit -m "Add Blueprint-VX candle-by-candle replay"`.
