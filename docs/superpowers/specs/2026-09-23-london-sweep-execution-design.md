# London Sweep Execution Model

## Objective

Add a paper-only execution model for BTC/USD that follows the supplied
Blueprint Edge trade rules while preserving the current Jev battery, Alpaca
paper/live gates, dashboard account telemetry, and hard risk engine.

The first version uses a 15-minute bias, a 1-minute setup structure, and
5-second execution bars. It trades only inside a configurable London session
window in UTC. It is an execution model, not a claim of profitability.

## Agreed behavior

- The latest confirmed 15-minute sweep before the London session establishes
  bias. A low sweep that closes back inside creates long bias; a high sweep
  that closes back inside creates short bias.
- A sweep is a wick beyond a confirmed fractal high or low followed by a close
  back inside that fractal range.
- An opposite confirmed 15-minute sweep during the session invalidates the
  current bias.
- A valid execution setup is a 1-minute sweep in the active bias direction.
  The first 1-minute refinement candle after the sweep supplies the trigger.
- Trades trigger when a 5-second bar breaks the refinement candle in the bias
  direction. A trigger that occurs after invalidation or session close expires.
- Multiple setups are allowed in one session, but only one position may be
  open at a time.
- Jev filters deterministic setups using regime and execution-quality
  judgments. Jev does not calculate levels, stops, targets, or quantity.
- Each trade risks a fixed maximum of $5, subject to every existing hard risk
  cap. Quantity is derived from entry-to-stop distance.
- The stop is beyond the sweep extreme. The target is 3R. At 1R, move the
  stop to break-even. Near 2.5R, enable a profit-protection stop.
- A short entry requires permitted inventory. The paper cash account must not
  create a naked short.

## Components and boundaries

### `market_structure.py`

Aggregates Alpaca trade data into 15-minute, 1-minute, and 5-second OHLC bars.
Maintains confirmed fractal levels and emits sweep candidates only after the
close-back-inside confirmation. It owns candle timestamps and session-boundary
alignment; it does not place orders or call Jev.

### `session_strategy.py`

Tracks the London session state, pre-session bias, bias invalidation, active
setup, refinement candle, and setup expiry. It returns a typed setup or a
reason for no setup. It has no Alpaca or network dependency and is deterministic
over a candle fixture.

### `execution/sweep.py`

Owns the position lifecycle: risk-based quantity, entry submission, stop,
target, break-even, profit protection, and completion. It delegates final
permission to the existing risk engine and execution client. It records order
identifiers, accepted/rejected status, and the reason for every transition.

### `strategy.py`

Remains the policy boundary. The sweep setup is deterministic; `apply_strategy`
can veto or modify a candidate only within hard risk limits. Jev is used as a
quality filter, not as the source of arithmetic or price levels.

### `limits.py` and `risk.py`

Remain authoritative. The new model cannot raise position, loss, drawdown,
stale-data, API-error, leverage, or latency caps. The $5 trade-risk target is
an additional strategy constraint.

### Dashboard and log

Expose session, bias, sweep level, refinement trigger, entry, stop, target,
R progress, invalidation reason, Jev filter result, Alpaca order status, and
the Alpaca account balance already used by the dashboard.

## Data flow

1. Read Alpaca trades and account state.
2. Update 15-minute, 1-minute, and 5-second bars.
3. Confirm pre-session bias and any opposite-bias invalidation.
4. Detect a 1-minute sweep and create a refinement trigger.
5. On a 5-second break, ask Jev about regime and execution quality.
6. Apply the deterministic policy and hard risk veto.
7. Submit a paper order with the calculated quantity and protective exits.
8. Manage the position through break-even, profit protection, target, or stop.
9. Log every state transition and publish the account-linked dashboard state.

## Failure handling

- Late Jev or stale data means no new entry; the current setup remains pending
  only until its explicit expiry, never indefinitely.
- A rejected entry is recorded with the Alpaca response and does not create an
  internal position.
- A rejected protective order is a hard execution fault and invokes the
  existing safety ladder.
- Session close cancels pending entries and manages or closes an active paper
  position according to the configured session policy.
- Restarts begin a fresh in-memory structure state and clearly mark the reset
  in the log; they do not invent historical candles.

## Testing

Add deterministic tests for:

- Confirmed long and short 15-minute sweeps.
- Wick-through without close-back-inside rejection.
- Bias invalidation by an opposite sweep.
- 1-minute sweep and refinement-candle trigger formation.
- 5-second break triggering exactly once.
- Expiry, session close, and one-position enforcement.
- Quantity sizing to a $5 maximum loss.
- Stop, 1R break-even, 2.5R profit protection, and 3R target transitions.
- Jev veto, late decision, stale data, rejected order, and protective-order
  failure behavior.
- Existing full test suite remaining green.

## Scope boundary

This branch does not enable live trading, change hard risk caps, add new
markets, or claim that the transcript's rules produce an edge. It establishes
a paper-testable execution model whose assumptions and outcomes are visible in
the dashboard and log.
