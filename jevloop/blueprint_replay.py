"""Read-only Blueprint-VX replay artifacts and candle chart payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from .blueprint_metrics import calculate_metrics
from .execution.sweep import DEFAULT_EXECUTION_MODEL_TAG
from .historical_replay import _aggregate, replay_historical_range
from .market_structure import Bar
from .replay import replay_range

BLUEPRINT_VX_TAG = "blueprint-VX"
LONDON_SWEEP_TAG = DEFAULT_EXECUTION_MODEL_TAG
PROXY_NOTICE = "Historical execution uses completed 1-minute bars as a 1m trigger proxy; exact 5-second or tick fills are not reconstructed."


def canonical_model_tag(tag: str) -> str:
    value = tag.strip().lower()
    if value in {"blueprint-vx", "london-sweep-v1"}:
        return LONDON_SWEEP_TAG
    raise ValueError("unsupported execution model tag; use blueprint-VX")


def public_model_tag(tag: str) -> str:
    canonical_model_tag(tag)
    return BLUEPRINT_VX_TAG


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _bar_from_payload(payload: dict[str, Any]) -> Bar:
    return Bar(
        float(payload["timestamp"]),
        int(payload.get("interval_s", 60)),
        float(payload["open"]),
        float(payload["high"]),
        float(payload["low"]),
        float(payload["close"]),
        float(payload.get("volume", 0.0)),
    )


def _bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "timestamp": bar.start_ts,
        "timestamp_utc": _iso(bar.start_ts),
        "interval_s": bar.interval_s,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _normalise_lifecycle(row: dict[str, Any]) -> list[dict[str, Any]]:
    lifecycle = row.get("lifecycle")
    if lifecycle:
        return [
            {
                "event": str(item.get("event", "hold")).lower().replace("-", "_"),
                "timestamp": float(item.get("timestamp", row.get("exit_ts") or row.get("entry_ts") or 0)),
                "timestamp_utc": item.get("timestamp_utc") or _iso(item.get("timestamp")),
                "price": item.get("price"),
            }
            for item in lifecycle
        ]
    timestamps = [row.get("entry_ts"), row.get("exit_ts")]
    return [
        {
            "event": str(event).lower().replace("-", "_"),
            "timestamp": float(timestamps[min(index, len(timestamps) - 1)] or 0),
            "timestamp_utc": _iso(timestamps[min(index, len(timestamps) - 1)]),
            "price": row.get("exit_price") if event in {"stop", "target"} else None,
        }
        for index, event in enumerate(row.get("transitions") or [])
    ]


def _fallback_bars(row: dict[str, Any]) -> list[dict[str, Any]]:
    entry_ts = float(row.get("entry_ts") or 0)
    exit_ts = float(row.get("exit_ts") or entry_ts + 60)
    entry = float(row.get("entry_price") or 0)
    stop = float(row.get("stop_price") or entry)
    exit_price = float(row.get("exit_price") or entry)
    values = [
        (entry_ts - 120, entry, max(entry, exit_price), min(entry, stop), entry),
        (entry_ts - 60, entry, max(entry, exit_price), min(entry, stop), entry),
        (entry_ts, entry, max(entry, exit_price), min(entry, stop), entry),
        (exit_ts, entry, max(entry, exit_price), min(entry, stop), exit_price),
    ]
    return [
        {
            "timestamp": timestamp,
            "timestamp_utc": _iso(timestamp),
            "interval_s": 60,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 0.0,
        }
        for timestamp, open_price, high, low, close in values
    ]


def _markers(row: dict[str, Any], lifecycle: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = [
        ("intent", row.get("intent_ts"), row.get("intent_price"), "Intent"),
        ("s1", row.get("s1_ts"), row.get("s1_price"), "S1"),
        ("aoi", row.get("aoi_ts"), row.get("aoi_price"), "AOI"),
        ("s2", row.get("signal_ts"), row.get("entry_price"), "S2"),
        ("x", row.get("entry_ts"), row.get("entry_price"), "X / entry"),
    ]
    for event in lifecycle:
        name = str(event.get("event", "")).lower().replace("-", "_")
        if name in {"break_even", "breakeven"}:
            entries.append(("break_even", event.get("timestamp"), event.get("price") or row.get("entry_price"), "Break-even"))
        elif name == "profit_lock":
            price = event.get("price")
            if price is None:
                entry = float(row.get("entry_price") or 0.0)
                stop = float(row.get("stop_price") or entry)
                risk = abs(entry - stop)
                direction = str(row.get("direction", "long")).lower()
                price = entry + 2.0 * risk if direction == "long" else entry - 2.0 * risk
            entries.append(("profit_lock", event.get("timestamp"), price, "Profit-lock"))
        elif name == "target":
            entries.append(("target", event.get("timestamp"), event.get("price") or row.get("target_price"), "Target"))
        elif name == "stop":
            entries.append(("stop", event.get("timestamp"), event.get("price") or row.get("stop_price"), "Stop"))
    return [
        {
            "kind": kind,
            "label": label,
            "timestamp": timestamp,
            "timestamp_utc": _iso(timestamp),
            "price": price,
        }
        for kind, timestamp, price, label in entries
        if timestamp is not None and price is not None
    ]


def _levels(row: dict[str, Any]) -> list[dict[str, Any]]:
    entry = float(row.get("entry_price") or 0)
    stop = float(row.get("stop_price") or entry)
    risk = abs(entry - stop)
    direction = str(row.get("direction", "long")).lower()
    expansion = entry + 2 * risk if direction == "long" else entry - 2 * risk
    return [
        {"label": "EX / invalidation", "kind": "invalidation", "price": stop},
        {"label": "PX / internal", "kind": "internal", "price": entry},
        {"label": "EP / expansion", "kind": "expansion", "price": expansion},
        {"label": "Target", "kind": "target", "price": float(row.get("target_price") or entry)},
    ]


def _reference_trade(row: dict[str, Any]) -> dict[str, Any]:
    outcome = str(row.get("outcome", "open")).upper()
    return {
        **row,
        "pair": row.get("symbol", "BTC/USD"),
        "side": str(row.get("direction", "long")).upper(),
        "result": outcome,
        "execution_tag": row.get("execution_model_tag", BLUEPRINT_VX_TAG),
        "trigger_proxy": row.get("resolution", "1m trigger proxy"),
        "entry_time_utc": _iso(row.get("entry_ts")),
        "exit_time_utc": _iso(row.get("exit_ts")),
        "intent_time_utc": _iso(row.get("intent_ts")),
        "s1_time_utc": _iso(row.get("s1_ts")),
        "aoi_time_utc": _iso(row.get("aoi_ts")),
        "s2_time_utc": _iso(row.get("signal_ts")),
    }


@dataclass
class BlueprintRun:
    run_id: str
    result: dict[str, Any]
    charts: dict[str, dict[str, Any]]

    def chart(self, trade_id: str, timeframe: str) -> dict[str, Any]:
        if timeframe not in {"1m", "15m"}:
            raise ValueError("timeframe must be 1m or 15m")
        payload = self.charts.get(trade_id)
        if payload is None:
            raise KeyError("unknown trade_id")
        bars = payload["bars_1m"] if timeframe == "1m" else payload["bars_15m"]
        chart = payload["chart"]
        return {
            "ok": True,
            "chart": {**chart, "selected_timeframe": timeframe},
        }


def _prepare_result(raw: dict[str, Any], *, public_tag: str, run_id: str) -> BlueprintRun:
    output = dict(raw)
    output["run_id"] = run_id
    output["execution_model_tag"] = public_tag
    output["proxy_notice"] = PROXY_NOTICE
    risk_usd = 5.0
    charts: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for index, source_row in enumerate(raw.get("rows", []), start=1):
        row = dict(source_row)
        if not row.get("entry_ts"):
            rows.append(row)
            continue
        trade_id = f"{run_id}-trade-{index:04d}"
        row["run_id"] = run_id
        row["trade_id"] = trade_id
        row["execution_model_tag"] = public_tag
        row["public_model_tag"] = public_tag
        risk_usd = float(row.get("max_loss_usd") or risk_usd)
        lifecycle = _normalise_lifecycle(row)
        source_bars = row.pop("chart_bars", None) or _fallback_bars(row)
        bars_1m = [_bar_payload(_bar_from_payload(item)) for item in source_bars]
        bars_15m = [_bar_payload(bar) for bar in _aggregate([_bar_from_payload(item) for item in source_bars], 900)]
        row["lifecycle"] = lifecycle
        row.pop("public_model_tag", None)
        charts[trade_id] = {
            "trade": _reference_trade(row),
            "bars_1m": bars_1m,
            "bars_15m": bars_15m,
            "levels": _levels(row),
            "markers": _markers(row, lifecycle),
        }
        direction = str(row.get("direction", "long")).lower()
        entry = float(row.get("entry_price") or 0)
        risk = abs(entry - float(row.get("stop_price") or entry))
        aoi_a = entry + (0.618 * risk if direction == "long" else -0.618 * risk)
        aoi_b = entry + (0.79 * risk if direction == "long" else -0.79 * risk)
        charts[trade_id]["chart"] = {
            "candles": {"1m": bars_1m, "15m": bars_15m},
            "levels": [
                {"role": level["kind"], "name": level["label"], "price": level["price"]}
                for level in charts[trade_id]["levels"]
            ],
            "aoi": {"low": min(aoi_a, aoi_b), "high": max(aoi_a, aoi_b)},
            "markers": charts[trade_id]["markers"],
            "window_start_utc": bars_1m[0]["timestamp_utc"] if bars_1m else _iso(row.get("entry_ts")),
            "window_end_utc": bars_1m[-1]["timestamp_utc"] if bars_1m else _iso(row.get("exit_ts")),
        }
        rows.append(row)
    output["rows"] = rows
    summary = dict(raw.get("summary", {}))
    summary["metrics"] = calculate_metrics(rows, risk_usd)
    summary["breakevens"] = summary["metrics"]["breakevens"]
    summary["wins"] = summary["metrics"]["wins"]
    summary["losses"] = summary["metrics"]["losses"]
    output["summary"] = summary
    output["trades"] = [_reference_trade(row) for row in rows if row.get("entry_ts")]
    cumulative = []
    running_r = 0.0
    for row in output["trades"]:
        if row.get("r_multiple") is None:
            continue
        running_r += float(row["r_multiple"])
        cumulative.append({"date": row.get("date"), "r": running_r})
    output["cumulative"] = cumulative
    output["bars"] = sum(len(chart["bars_1m"]) for chart in charts.values())
    output["request"] = {
        "start_utc": f"{raw.get('start')}T00:00:00Z",
        "end_utc": f"{raw.get('end')}T23:59:59Z",
    }
    output["resolution"] = "1m trigger proxy"
    return BlueprintRun(run_id, output, charts)


def build_fixture_run(start, end, tag: str = BLUEPRINT_VX_TAG) -> BlueprintRun:
    public_model_tag(tag)
    raw = replay_range(start, end, LONDON_SWEEP_TAG)
    return _prepare_result(raw, public_tag=BLUEPRINT_VX_TAG, run_id=uuid4().hex)


def build_historical_run(start, end, tag: str = BLUEPRINT_VX_TAG) -> BlueprintRun:
    public_model_tag(tag)
    raw = replay_historical_range(start, end, LONDON_SWEEP_TAG, include_artifacts=True)
    return _prepare_result(raw, public_tag=BLUEPRINT_VX_TAG, run_id=uuid4().hex)


class BlueprintRunStore:
    """Thread-safe in-memory store for completed read-only replay runs."""

    def __init__(self) -> None:
        self._runs: dict[str, BlueprintRun] = {}
        self._lock = RLock()

    def put(self, run: BlueprintRun) -> BlueprintRun:
        with self._lock:
            self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> BlueprintRun | None:
        with self._lock:
            return self._runs.get(run_id)
