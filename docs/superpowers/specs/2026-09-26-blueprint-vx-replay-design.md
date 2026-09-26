# Blueprint-VX Candle-by-Candle Replay

## Goal

Add a candle-by-candle replay workspace for the existing London-sweep execution
model under its canonical display/tag name, `blueprint-VX`. The workspace must
match the current `mql5-ea/dashboard/isx-replay.html` visual and interaction
language, while leaving that legacy page and the existing staging replay page
unchanged.

The implementation lives on `feature/london-sweep-execution`, which tracks
`origin/feature/london-sweep-execution`. `blueprint-VX` is an alias for the
existing `london-sweep-v1` model; the model rules and outcomes are not changed.

## Page and visual contract

The new page is `dashboard/blueprint-vx-replay.html`. Its DOM order and visual
tokens follow the reference page: header, hero, UTC/source controls, summary
cards, quality/risk metrics, cumulative-result chart, candle-by-candle chart,
trade ledger, selected-trade detail/lifecycle panel, and background progress
toast. It uses the reference gradients, glass cards, typography, spacing,
breakpoints, colors, chart dimensions, labels, and responsive behavior.

Reference styles and interaction helpers are extracted into assets used by the
new page where that can be done without editing the legacy `isx-replay.html`.
The reference page itself is not modified.

The page labels historical executions as `1m trigger proxy`, offers the same
`1m trigger` and `15m context` selector, and keeps the reference loading,
empty, unavailable, and error states.

## Replay data flow

The browser submits only replay parameters to read-only replay endpoints. A
background job endpoint returns a stable `run_id`; the page polls job status and
renders the completed result. Each trade has a stable `trade_id` and carries
the execution tag `blueprint-VX` while invoking the existing London-sweep
implementation.

The chart endpoint is:

```text
/api/replay/{run_id}/trades/{trade_id}/chart
```

It returns completed 1-minute OHLC bars for the trigger view and aggregated
15-minute OHLC bars for context. The response includes the AOI range, EX/
invalidation, PX/internal, EP/expansion, and target levels plus Intent, S1,
AOI, S2/X, break-even, profit-lock, and stop markers. No endpoint submits or
modifies an order.

## Analytics contract

Analytics are computed server-side from closed replay trades and lifecycle
events, then returned with the replay summary. The page renders the reference
metric labels and definitions:

- actual losses exclude 0R break-even exits;
- win, loss, and break-even rates use closed trades as the denominator;
- expectancy is mean R across closed trades;
- profit factor is gross positive R divided by absolute gross negative R;
- average win and average loss are reported in R;
- current, maximum win, maximum loss, and maximum non-positive streaks are
  calculated in chronological trade order;
- drawdown is peak-to-trough cumulative R and USD;
- target exits, stop losses, stop break-even exits, stop profit-lock exits,
  break-even moves, and profit-lock moves are counted from lifecycle events;
- maximum trades per day and maximum losses per day are calculated from the
  replayed dates.

Empty sets use the reference page's em dash/zero behavior and must not produce
NaN or divide-by-zero output.

## Isolation and compatibility

The existing `dashboard/isx-replay.html` is not edited. The existing
`dashboard/replay.html` behavior is not changed. Blueprint-VX uses its own
page, API job namespace, stable model alias, and chart payload adapters. The
pre-existing unrelated working-tree edit in `jevloop/loop.py` is preserved and
not folded into this feature.

## Verification

Add deterministic tests for:

1. `blueprint-VX` aliasing to the London-sweep model and propagating through
   run and trade tags;
2. analytics values, including losses versus break-even exits, rates,
   expectancy, profit factor, averages, streaks, drawdown, lifecycle counts,
   and daily maxima;
3. chart payload OHLC bars, levels, marker colors/types, timeframe selection,
   and trade/run identifiers;
4. lazy chart loading and background job completion/error states;
5. no order endpoint calls and no changes to the legacy replay page.

Run the focused replay tests and the complete test suite. Start the local
server on port 8767, compare the new page beside the reference page, and
verify the page at desktop and narrow responsive widths.
