# MT5 hybrid ISX bridge

## Problem

The deterministic ISX engine currently consumes Python `Candle` objects and
the Alpaca loop owns its execution adapter. An MT5 Expert Advisor needs the
same close-confirmed decisions while MT5 remains responsible for broker
prices, account state, and order submission.

## Decision

Use a loopback HTTP bridge hosted by `jevloop serve`:

```
MT5 EA -- completed H4/H1/M15 bars + bid/ask --> Python bridge
MT5 EA <-- ISX decision, anchors, stop, 4R target -- Python ISX engine
MT5 EA -- local risk gates + tagged order ----------------> MT5 broker
```

The Python endpoint is read-only. It never calls an MT5 or Alpaca order API.
The EA defaults to shadow mode and live order submission remains an explicit
EA input. The final risk veto is inside the EA so a bridge response cannot
bypass terminal-side safety checks.

## Protocol

`POST /api/mt5/isx/decision` accepts a `session_id`, MT5 symbol, completed
H4/H1/M15 arrays, and an optional bid/ask trigger snapshot. Times may be ISO
8601 UTC or Unix seconds. A session owns one `ISXEngine`, so repeated candle
sets are idempotent and a fresh session can replay its submitted history to
reconstruct state.

The response includes the ISX phase, action, setup ID, execution tag,
observability fields, heartbeat, and a derived execution proposal. The
proposal is permission only: it contains side, entry, EX stop, and the
configured R target. The response always marks `live_execution_allowed` false.

`GET /api/mt5/status` exposes bridge heartbeats and the last closed candle for
local observability. If `JEV_MT5_BRIDGE_TOKEN` is set, the decision endpoint
requires the matching `X-Jev-Bridge-Token` header. The server binds to
127.0.0.1 by default.

## EA responsibilities

The EA will use `CopyRates(symbol, timeframe, 1, count, rates)` so index zero
is never the active candle. It will poll the bridge on a timer, include a
bounded history warm-up, and reject malformed or stale responses. The EA will
deduplicate by setup ID, persist the last setup in a terminal global variable,
and refuse to submit if shadow mode is enabled, trading is disabled, spread is
too wide, stop distance is invalid, or a matching ISX position already exists.

Orders use a dedicated magic number and an execution comment containing the
model tag and setup ID. Position management moves the stop to break-even at
1R and the profit lock at 2R, with a default 4R target. Any future live mode
must be tested in MT5 Strategy Tester and on a demo account before enabling
the EA input.

## Failure behavior

Bridge timeout, HTTP errors, invalid JSON, missing history, incomplete bars,
symbol mismatch, or stale trigger data all produce a local EA `STAND_DOWN`.
No retry may submit an order twice. Python bridge errors are returned as
structured JSON and are visible in the existing server logs/dashboard status.

## Validation

Python tests cover payload validation, UTC normalization, session idempotence,
X proposal geometry, and the read-only contract. A static MQL5 protocol test
will verify the EA endpoint, default shadow mode, closed-bar shift, token
header, tag, and risk-lock inputs. A manual smoke test will POST the existing
ISX fixture bars to the bridge and assert `STAND_DOWN`/`ISX_X` responses
without any order side effect.
