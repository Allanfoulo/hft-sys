# ISX replay session filter

## Goal

Let the replay compare ISX execution quality by UTC participation window without changing the Intent -> S1 -> AOI -> S2 -> X structure rules. The selected window controls whether an otherwise valid X trigger may create a replay trade.

## Session definitions

The first implementation uses fixed UTC windows:

- `all`: 00:00-24:00 UTC, no filter.
- `asia`: 00:00-08:00 UTC.
- `london`: 08:00-13:00 UTC.
- `new_york`: 13:00-21:00 UTC.
- `custom`: a user-provided UTC start and end time, with overnight windows supported when the end is earlier than the start.

The labels are UTC labels. They do not follow daylight-saving changes automatically. The UI will state that explicitly.

## Data flow

`ReplayRequest` gains a validated session filter and optional custom UTC times. `ReplayEngine` evaluates the normal completed-bar sequence and, at the exact 1-minute trigger-proxy timestamp where X would create a trade, checks the session filter. Out-of-window X signals are recorded as skipped signals and do not enter the lifecycle ledger or cumulative P&L.

The replay result records the filter and skipped-signal count. Trade execution tags remain unchanged, while the run metadata and dashboard labels include the selected session.

## UI

Add a `Session` dropdown beside the data-source and instrument controls. It contains All sessions, Asia, London, New York, and Custom UTC. Custom mode reveals UTC start/end time inputs. A run uses the selected values in its request. The notice and cumulative-chart label identify the active session filter.

## Validation and safety

- Session names are an enum; unknown values are rejected.
- Fixed windows use half-open intervals `[start, end)`.
- Custom times must be valid `HH:MM` UTC values and cannot be equal.
- An overnight custom window is valid and matches either side of midnight.
- Session filtering is applied only after ISX X permission is complete; it cannot create, confirm, or alter structure.
- Existing risk vetoes and replay read-only behavior are unchanged.

## Tests

Add deterministic tests for each fixed window, the half-open boundary, overnight custom windows, invalid custom values, request round-tripping, and a replay where an otherwise valid X trigger is skipped outside the selected session. Existing no-filter replay tests must retain their results.
