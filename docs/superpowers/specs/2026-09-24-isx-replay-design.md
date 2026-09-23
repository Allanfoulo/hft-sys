# ISX Replay Design

Status: architecture approved; pending written-spec review.

## Goal

Add a read-only replay workspace for the ISX execution model. A replay accepts
a UTC date range, pair, data source, and lifecycle settings, then returns a
deterministic ledger, summary metrics, cumulative-result series, and clickable
trade details. It must never submit an Alpaca order.

The replay uses the existing close-confirmed ISX structure primitives. Its
replay cadence is:

```text
15-minute Intent/bias -> 1-minute S1 -> 61.8%-79.0% retracement AOI
-> 1-minute S2 -> X
```

There is no sweep, stop-hunt, liquidity-grab, indicator, or tick-level claim.
The 1-minute S2 close is a clearly labelled historical trigger proxy.

## Architecture

The implementation lives on `feature/isx-replay` and is split into focused
modules:

- `jevloop/replay/models.py` contains typed request, bar, setup, lifecycle,
  trade, summary, and result values.
- `jevloop/replay/data.py` defines a read-only historical-bar provider, a
  deterministic fixture provider, and a paginated Alpaca 1-minute crypto
  provider. The provider resamples completed UTC 1-minute bars into completed
  15-minute bars for bias evaluation.
- `jevloop/replay/engine.py` evaluates the setup sequence and trade lifecycle
  without importing the order executor or calling an order endpoint.
- `jevloop/replay/service.py` validates requests, selects a provider, applies
  pagination/range limits, tags trades, and serializes results for the server.
- `jevloop/serve.py` exposes a read-only `POST /api/replay` endpoint alongside
  the existing static dashboard files. It returns structured validation,
  provider, and replay errors and never accepts an execution command.
- `dashboard/replay.html` implements the selected run-and-inspect layout: UTC
  controls on the left, selected-trade detail on the right, summary cards,
  cumulative chart, and a clickable ledger.

The browser sends only replay parameters to the local server. Alpaca keys stay
server-side. Fixture mode never needs credentials or network access.

## Structural replay rules

1. Only completed bars are considered; bars are sorted and de-duplicated by
   UTC timestamp.
2. The 15-minute bias uses the existing wick-pivot and close-confirmed BOS
   rules. A neutral or unconfirmed structure produces no setup.
3. A new directional 15-minute intent freezes EX, PX, and EP from the leg that
   produced the intent break. A later setup gets a new deterministic setup ID.
4. S1 is the first completed 1-minute close-confirmed structural shift against
   Intent. S1 does not redefine the 15-minute anchors.
5. After S1, the frozen range maps to the existing 61.8%-79.0% AOI. An AOI
   touch alone cannot create a trade; a close through EX/1.0 invalidates it.
6. S2 is the first completed 1-minute close-confirmed shift back with Intent
   after AOI qualification. S2 before AOI is ignored.
7. X occurs once per setup at the next available 1-minute bar open after the
   S2 close. This is the `1m-trigger-proxy` execution timestamp and is not
   represented as an exact tick or 5-second fill.

## Lifecycle and accounting

The defaults are intentionally explicit and are request settings in the UI:

- stop: frozen EX/1.0 invalidation boundary;
- break-even: move the stop to entry after +1R;
- profit lock: at +2R, move the stop to +1R;
- target: 4R by default, with 3R available as an alternate setting;
- risk basis: configurable USD risk per trade, default $100, so P&L is
  `realized_R * risk_usd`;
- if a completed 1-minute bar touches both the active stop and target, stop is
  resolved first for a conservative result;
- if neither boundary is reached before the requested range ends, the trade is
  `OPEN` with no realized P&L and its current mark is recorded.

Each trade contains date, pair, side, entry/exit UTC, entry/exit prices,
result, P&L, R multiple, setup ID, execution tag, trigger proxy label, stop,
target, and lifecycle events. Tags use a stable form such as
`ISX-REPLAY-BTCUSD-20260924T120000-0001`.

## Request and response contract

`POST /api/replay` accepts JSON:

```json
{
  "source": "fixture",
  "symbol": "BTC/USD",
  "start_utc": "2026-09-01T00:00:00Z",
  "end_utc": "2026-09-02T00:00:00Z",
  "target_r": 4.0,
  "breakeven_r": 1.0,
  "profit_lock_trigger_r": 2.0,
  "profit_lock_r": 1.0,
  "risk_usd": 100.0
}
```

The service rejects reversed or naive dates, unsupported symbols/sources,
non-positive lifecycle values, and ranges larger than the configured replay
window. Alpaca pagination follows `next_page_token` and processes bars in
chunks; fixture data uses the same interface, so the engine sees identical
inputs regardless of provider. The response contains request metadata,
`proxy_notice`, summary metrics (sessions, trades, wins, losses, total R, P&L),
the cumulative series, and the full ledger/detail records.

## UI behavior

The replay page shows:

- UTC start/end inputs with the active range echoed back;
- source selector with a visible offline-fixture or Alpaca label;
- pair and lifecycle settings, with 4R and +1R defaults;
- Run Replay, loading, validation, network, and empty-result states;
- an always-visible `1m-trigger-proxy` notice for historical runs;
- summary cards and a cumulative R/P&L chart;
- a ledger with the requested columns and execution tags;
- clickable rows that populate the detail panel with 15m bias, S1, AOI,
  S2/X, entry, stop, target, lock, break-even, exit, and outcome events.

## Safety and isolation

The replay service has no reference to `submit_market_order`,
`submit_limit_order`, `cancel_all_orders`, or the live/paper trading loop. The
Alpaca provider is read-only and tests will assert that no order endpoint is
called. Replay tags are descriptive evidence of the model that generated a
historical result; they do not create or submit broker orders.

## Verification

Fixture tests will cover UTC validation, completed-bar filtering, deterministic
replay/idempotence, Intent/S1/AOI/S2 ordering, AOI-only rejection, EX
invalidation, +1R break-even, +2R profit lock, 4R target, conservative same-bar
resolution, open outcomes, stable tags, pagination, Alpaca error handling, and
the no-order isolation guard. Existing repository tests must continue to pass.
