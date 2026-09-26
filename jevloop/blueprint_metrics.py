"""Pure quality and risk analytics for Blueprint-VX replay results."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence


def _closed(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("r_multiple") is not None]


def _event_names(row: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for event in row.get("transitions") or row.get("lifecycle") or []:
        if isinstance(event, Mapping):
            value = event.get("event") or event.get("kind") or ""
        else:
            value = event
        names.add(str(value).strip().lower().replace("-", "_"))
    return names


def calculate_metrics(rows: Sequence[Mapping[str, Any]], risk_usd: float) -> dict[str, Any]:
    """Calculate the metrics displayed by the reference replay workspace."""
    closed = _closed(rows)
    r_values = [float(row.get("r_multiple") or 0.0) for row in closed]
    wins = [value for value in r_values if value > 0]
    losses = [value for value in r_values if value < 0]
    breakevens = [value for value in r_values if value == 0]
    denominator = len(r_values)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    max_win_streak = 0
    max_loss_streak = 0
    max_non_positive_streak = 0
    current_streak = 0
    current_streak_type = "NONE"
    win_streak = 0
    loss_streak = 0
    non_positive_streak = 0
    running_r = 0.0
    peak_r = 0.0
    max_drawdown_r = 0.0
    for value in r_values:
        running_r += value
        peak_r = max(peak_r, running_r)
        max_drawdown_r = max(max_drawdown_r, peak_r - running_r)
        if value > 0:
            win_streak += 1
            loss_streak = 0
            non_positive_streak = 0
            current_streak = win_streak
            current_streak_type = "WIN"
        elif value < 0:
            win_streak = 0
            loss_streak += 1
            non_positive_streak += 1
            current_streak = loss_streak
            current_streak_type = "LOSS"
        else:
            win_streak = 0
            loss_streak = 0
            non_positive_streak += 1
            current_streak = non_positive_streak
            current_streak_type = "BREAK_EVEN"
        max_win_streak = max(max_win_streak, win_streak)
        max_loss_streak = max(max_loss_streak, loss_streak)
        max_non_positive_streak = max(max_non_positive_streak, non_positive_streak)

    daily_trades = Counter(str(row.get("date", "")) for row in closed)
    daily_losses = Counter(
        str(row.get("date", ""))
        for row, value in zip(closed, r_values)
        if value < 0
    )
    target_exits = sum(1 for row in closed if str(row.get("outcome", "")).lower() == "target")
    stop_rows = [row for row in closed if str(row.get("outcome", "")).lower() == "stop"]
    event_sets = [_event_names(row) for row in closed]
    stop_losses = sum(1 for row, value in zip(stop_rows, [float(row.get("r_multiple") or 0.0) for row in stop_rows]) if value < 0)
    stop_breakevens = sum(1 for row, value in zip(stop_rows, [float(row.get("r_multiple") or 0.0) for row in stop_rows]) if value == 0)
    stop_profit_locks = sum(1 for row, value in zip(stop_rows, [float(row.get("r_multiple") or 0.0) for row in stop_rows]) if value > 0)

    return {
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(breakevens),
        "win_rate": len(wins) / denominator if denominator else 0.0,
        "loss_rate": len(losses) / denominator if denominator else 0.0,
        "breakeven_rate": len(breakevens) / denominator if denominator else 0.0,
        "expectancy_r": sum(r_values) / denominator if denominator else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "avg_win_r": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_r": sum(losses) / len(losses) if losses else 0.0,
        "current_streak": current_streak,
        "current_streak_type": current_streak_type,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "max_non_positive_streak": max_non_positive_streak,
        "max_drawdown_r": max_drawdown_r,
        "max_drawdown_usd": max_drawdown_r * float(risk_usd),
        "target_exits": target_exits,
        "stop_losses": stop_losses,
        "stop_breakevens": stop_breakevens,
        "stop_profit_locks": stop_profit_locks,
        "break_even_moves": sum("break_even" in events or "breakeven" in events for events in event_sets),
        "profit_lock_moves": sum("profit_lock" in events or "profitlock" in events for events in event_sets),
        "max_trades_per_day": max(daily_trades.values(), default=0),
        "max_losses_per_day": max(daily_losses.values(), default=0),
    }
