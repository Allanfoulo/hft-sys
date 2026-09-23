"""Authenticated Alpaca historical replay for the sweep execution model.

The crypto bars endpoint provides minute aggregates.  This adapter derives the
15m bias and 1m setup from those bars and uses the next 1m bar as the trigger
proxy.  It reports that resolution explicitly so the UI never presents a
minute replay as exact tick or 5s execution.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import requests

from .execution.sweep import DEFAULT_EXECUTION_MODEL_TAG, SweepPosition, build_trade_plan
from .market_structure import Bar, confirmed_fractals, detect_sweeps
from .replay import REPLAY_SYMBOL
from .session_strategy import LondonSweepEngine

DATA_BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"


class HistoricalReplayError(RuntimeError):
    """Historical data could not be loaded or replayed."""


def _utc_datetime(day: date, end: bool = False) -> datetime:
    if end:
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bar_from_payload(item: dict[str, Any]) -> Bar:
    timestamp = item.get("t")
    if not timestamp:
        raise HistoricalReplayError("Alpaca bar has no timestamp")
    dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    return Bar(
        dt.timestamp(),
        60,
        float(item["o"]),
        float(item["h"]),
        float(item["l"]),
        float(item["c"]),
        float(item.get("v", 0.0)),
    )


def _fetch_minute_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    api_key: str,
    secret_key: str,
    *,
    request_get=requests.get,
) -> list[Bar]:
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }
    params: dict[str, Any] = {
        "symbols": symbol,
        "timeframe": "1Min",
        "start": _iso(start),
        "end": _iso(end),
        "limit": 10000,
        "sort": "asc",
    }
    bars: list[Bar] = []
    while True:
        try:
            response = request_get(DATA_BARS_URL, headers=headers, params=params, timeout=20)
        except requests.RequestException as exc:
            raise HistoricalReplayError(f"Alpaca historical data request failed: {exc}") from exc
        if response.status_code >= 400:
            raise HistoricalReplayError(
                f"Alpaca historical data returned HTTP {response.status_code}: {response.text[:240]}"
            )
        payload = response.json()
        for item in payload.get("bars", {}).get(symbol, []):
            bars.append(_bar_from_payload(item))
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return bars


def _aggregate(bars: list[Bar], interval_s: int) -> list[Bar]:
    grouped: dict[int, list[Bar]] = {}
    for bar in bars:
        start = int(bar.start_ts // interval_s) * interval_s
        grouped.setdefault(start, []).append(bar)
    result: list[Bar] = []
    for start in sorted(grouped):
        group = grouped[start]
        result.append(
            Bar(
                float(start),
                interval_s,
                group[0].open,
                max(item.high for item in group),
                min(item.low for item in group),
                group[-1].close,
                sum(item.volume for item in group),
            )
        )
    return result


def _row_from_plan(plan, signal, transition, *, day: date, symbol: str) -> dict[str, Any]:
    is_profit = transition.event == "target"
    r_multiple = 3.0 if is_profit else -1.0 if transition.event == "stop" else None
    pnl = plan.max_loss_usd * r_multiple if r_multiple is not None else None
    return {
        "ok": True,
        "date": day.isoformat(),
        "session": "08:00-11:00 UTC",
        "symbol": symbol,
        "execution_model_tag": plan.execution_model_tag,
        "bias": plan.direction,
        "setup_id": plan.setup_id,
        "signal_ts": signal.timestamp,
        "entry_ts": signal.timestamp,
        "direction": plan.direction,
        "quantity": plan.quantity,
        "entry_price": plan.entry_price,
        "stop_price": plan.stop_price,
        "target_price": plan.target_price,
        "exit_ts": transition.timestamp,
        "exit_price": transition.state.exit_price,
        "outcome": transition.event,
        "profit_loss": "Profit" if is_profit else "Loss" if transition.event == "stop" else "Open",
        "simulated": False,
        "historical": True,
        "resolution": "1m trigger proxy",
        "r_multiple": r_multiple,
        "pnl_usd": pnl,
        "transitions": [transition.event],
    }


def replay_historical_range(
    start: date,
    end: date,
    execution_model_tag: str = DEFAULT_EXECUTION_MODEL_TAG,
    *,
    symbol: str = REPLAY_SYMBOL,
) -> dict[str, Any]:
    """Replay authenticated Alpaca minute bars over an inclusive date range."""
    if end < start:
        raise HistoricalReplayError("end date must be on or after start date")
    if (end - start).days > 31:
        raise HistoricalReplayError("historical replay is limited to 32 days per request")
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise HistoricalReplayError(
            "Alpaca historical replay needs ALPACA_API_KEY and ALPACA_SECRET_KEY in the branch environment"
        )

    # Six hours of warmup gives the confirmed 15m fractal calculation enough
    # context to establish a pre-session bias on the first requested day.
    bars_1m = _fetch_minute_bars(
        symbol,
        _utc_datetime(start) - timedelta(hours=6),
        _utc_datetime(end, end=True),
        api_key,
        secret_key,
    )
    if not bars_1m:
        raise HistoricalReplayError("Alpaca returned no 1m bars for the requested range")
    bars_1m.sort(key=lambda bar: bar.start_ts)
    bars_15m = _aggregate(bars_1m, 900)
    sweeps_15m = detect_sweeps(bars_15m, confirmed_fractals(bars_15m))
    sweeps_1m = detect_sweeps(bars_1m, confirmed_fractals(bars_1m))
    sweeps_by_start: dict[float, list] = {}
    for sweep in sweeps_1m:
        sweeps_by_start.setdefault(sweep.bar.start_ts, []).append(sweep)

    rows: list[dict[str, Any]] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        day_start = _utc_datetime(day)
        session_start = day_start + timedelta(hours=8)
        session_end = day_start + timedelta(hours=11)
        day_end = day_start + timedelta(days=1)
        engine = LondonSweepEngine()
        for sweep in sweeps_15m:
            if sweep.timestamp <= session_end.timestamp():
                engine.on_15m_sweep(sweep)
        day_bars = [bar for bar in bars_1m if day_start.timestamp() <= bar.start_ts < day_end.timestamp()]
        position: SweepPosition | None = None
        open_row: dict[str, Any] | None = None
        for bar in day_bars:
            # Manage an existing position before looking for another entry.
            if position is not None:
                transition = position.update(bar)
                if transition.state.status == "closed":
                    assert open_row is not None
                    open_row.update(
                        _row_from_plan(
                            position.state.plan,
                            open_row["_signal"],
                            transition,
                            day=day,
                            symbol=symbol,
                        )
                    )
                    open_row.pop("_signal", None)
                    rows.append(open_row)
                    position = None
                    open_row = None

            refinement = None
            if session_start.timestamp() <= bar.start_ts < session_end.timestamp():
                for sweep in sweeps_by_start.get(bar.start_ts, []):
                    engine.on_1m_sweep(sweep, position_open=position is not None)
                refinement = engine.on_1m_bar(bar, position_open=position is not None)
            # The minute bar that creates the refinement is not also used as
            # its trigger. The next minute is the documented 1m proxy for the
            # lower-resolution 5s/tick trigger.
            signal = (
                None
                if refinement is not None and refinement.refinement.start_ts == bar.start_ts
                else engine.on_5s_bar(bar, position_open=position is not None)
            )
            if signal is None or position is not None:
                continue
            # Historical Jev judgments are not reconstructed. The structural
            # rules are replayed and the missing model judgment is reported in
            # the source metadata instead of being silently fabricated.
            try:
                plan = build_trade_plan(signal, execution_model_tag=execution_model_tag)
            except ValueError:
                continue
            position = SweepPosition(plan)
            open_row = {"_signal": signal}

        if position is not None and open_row is not None:
            open_row.update(
                {
                    "ok": True,
                    "date": day.isoformat(),
                    "session": "08:00-11:00 UTC",
                    "symbol": symbol,
                    "execution_model_tag": position.state.plan.execution_model_tag,
                    "bias": position.state.plan.direction,
                    "setup_id": position.state.plan.setup_id,
                    "signal_ts": open_row["_signal"].timestamp,
                    "entry_ts": open_row["_signal"].timestamp,
                    "direction": position.state.plan.direction,
                    "quantity": position.state.plan.quantity,
                    "entry_price": position.state.plan.entry_price,
                    "stop_price": position.state.plan.stop_price,
                    "target_price": position.state.plan.target_price,
                    "exit_ts": None,
                    "exit_price": None,
                    "outcome": "open",
                    "profit_loss": "Open",
                    "simulated": False,
                    "historical": True,
                    "resolution": "1m trigger proxy",
                    "r_multiple": None,
                    "pnl_usd": None,
                    "transitions": [],
                }
            )
            open_row.pop("_signal", None)
            rows.append(open_row)

    closed = [row for row in rows if row["r_multiple"] is not None]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "execution_model_tag": execution_model_tag,
        "symbol": symbol,
        "fixture": False,
        "historical": True,
        "simulated": False,
        "resolution": "1m trigger proxy",
        "rows": rows,
        "summary": {
            "sessions": (end - start).days + 1,
            "setups": len(rows),
            "trades": len(rows),
            "wins": sum(1 for row in closed if row["r_multiple"] > 0),
            "losses": sum(1 for row in closed if row["r_multiple"] < 0),
            "open": sum(1 for row in rows if row["r_multiple"] is None),
            "total_r": sum(row["r_multiple"] for row in closed),
            "pnl_usd": sum(row["pnl_usd"] for row in closed),
        },
    }
