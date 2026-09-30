"""Typed request, lifecycle, and result values for ISX replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


MAX_REPLAY_DAYS = 90


class ReplaySource(str, Enum):
    FIXTURE = "fixture"
    ALPACA = "alpaca"
    MT5 = "mt5"


class ReplaySession(str, Enum):
    ALL = "all"
    ASIA = "asia"
    LONDON = "london"
    NEW_YORK = "new_york"
    CUSTOM = "custom"


_FIXED_SESSION_WINDOWS: dict[ReplaySession, tuple[int, int]] = {
    ReplaySession.ASIA: (0, 8 * 60),
    ReplaySession.LONDON: (8 * 60, 13 * 60),
    ReplaySession.NEW_YORK: (13 * 60, 21 * 60),
}


class ReplayResultKind(str, Enum):
    TARGET = "TARGET"
    STOP = "STOP"
    OPEN = "OPEN"
    INVALIDATED = "INVALIDATED"


class ReplaySide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class ReplayChartBar:
    """A completed OHLC bar exposed to the read-only chart replay."""

    timestamp: datetime
    timeframe: str
    open: float
    high: float
    low: float
    close: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp.isoformat().replace("+00:00", "Z"),
            "timeframe": self.timeframe,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }


@dataclass(frozen=True)
class ReplayChartMarker:
    timestamp: datetime
    kind: str
    label: str
    price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp.isoformat().replace("+00:00", "Z"),
            "kind": self.kind,
            "label": self.label,
            "price": self.price,
        }


@dataclass(frozen=True)
class ReplayChartLevel:
    name: str
    price: float
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "price": self.price, "role": self.role}


def _utc_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _utc_minutes(value: Any, name: str) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an HH:MM UTC time")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"{name} must be an HH:MM UTC time")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{name} must be an HH:MM UTC time") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{name} must be an HH:MM UTC time")
    return hour * 60 + minute


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class ReplayRequest:
    source: ReplaySource
    symbol: str
    start_utc: datetime
    end_utc: datetime
    target_r: float = 4.0
    breakeven_r: float = 1.0
    profit_lock_trigger_r: float = 2.0
    profit_lock_r: float = 1.0
    risk_usd: float = 100.0
    pivot_left: int = 1
    pivot_right: int = 1
    session: ReplaySession = ReplaySession.ALL
    session_start_utc: str | None = None
    session_end_utc: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ReplayRequest":
        if not isinstance(payload, dict):
            raise ValueError("replay body must be a JSON object")
        try:
            source = ReplaySource(str(payload.get("source", "fixture")).lower())
        except ValueError as exc:
            raise ValueError("source must be 'fixture', 'alpaca', or 'mt5'") from exc
        symbol = str(payload.get("symbol", "BTC/USD")).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        try:
            session = ReplaySession(str(payload.get("session", "all")).lower())
        except ValueError as exc:
            raise ValueError("session must be 'all', 'asia', 'london', 'new_york', or 'custom'") from exc
        session_start = payload.get("session_start_utc")
        session_end = payload.get("session_end_utc")
        if session is ReplaySession.CUSTOM:
            start_minutes = _utc_minutes(session_start, "session_start_utc")
            end_minutes = _utc_minutes(session_end, "session_end_utc")
            if start_minutes == end_minutes:
                raise ValueError("custom session start and end must be different")
            session_start = _format_minutes(start_minutes)
            session_end = _format_minutes(end_minutes)
        elif session is not ReplaySession.ALL:
            start_minutes, end_minutes = _FIXED_SESSION_WINDOWS[session]
            session_start = _format_minutes(start_minutes)
            session_end = _format_minutes(end_minutes)
        else:
            session_start = None
            session_end = None
        start = _utc_datetime(payload.get("start_utc"), "start_utc")
        end = _utc_datetime(payload.get("end_utc"), "end_utc")
        if end <= start:
            raise ValueError("end_utc must be after start_utc")
        if (end - start).total_seconds() > MAX_REPLAY_DAYS * 86400:
            raise ValueError(f"date range cannot exceed {MAX_REPLAY_DAYS} days")

        def number(name: str, default: float) -> float:
            try:
                value = float(payload.get(name, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be numeric") from exc
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
            return value

        target_r = number("target_r", 4.0)
        breakeven_r = number("breakeven_r", 1.0)
        lock_trigger = number("profit_lock_trigger_r", 2.0)
        lock_r = float(payload.get("profit_lock_r", 1.0))
        if lock_r < 0:
            raise ValueError("profit_lock_r cannot be negative")
        risk_usd = number("risk_usd", 100.0)
        if breakeven_r >= target_r:
            raise ValueError("breakeven_r must be below target_r")
        if lock_trigger >= target_r:
            raise ValueError("profit_lock_trigger_r must be below target_r")
        if lock_r >= target_r:
            raise ValueError("profit_lock_r must be below target_r")
        if lock_r > lock_trigger:
            raise ValueError("profit_lock_r cannot exceed profit_lock_trigger_r")
        try:
            pivot_left = int(payload.get("pivot_left", 1))
            pivot_right = int(payload.get("pivot_right", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("pivot strengths must be integers") from exc
        if pivot_left < 1 or pivot_right < 1:
            raise ValueError("pivot strengths must be at least one")
        return cls(
            source,
            symbol,
            start,
            end,
            target_r,
            breakeven_r,
            lock_trigger,
            lock_r,
            risk_usd,
            pivot_left,
            pivot_right,
            session,
            session_start,
            session_end,
        )

    def session_window(self) -> tuple[int, int] | None:
        if self.session is ReplaySession.ALL:
            return None
        if self.session is ReplaySession.CUSTOM:
            assert self.session_start_utc is not None and self.session_end_utc is not None
            return (
                _utc_minutes(self.session_start_utc, "session_start_utc"),
                _utc_minutes(self.session_end_utc, "session_end_utc"),
            )
        return _FIXED_SESSION_WINDOWS[self.session]

    def allows_timestamp(self, timestamp: datetime) -> bool:
        window = self.session_window()
        if window is None:
            return True
        start, end = window
        minute = timestamp.astimezone(timezone.utc).hour * 60 + timestamp.astimezone(timezone.utc).minute
        return (start <= minute < end) if start < end else (minute >= start or minute < end)

    def session_label(self) -> str:
        if self.session is ReplaySession.ALL:
            return "all sessions"
        if self.session is ReplaySession.CUSTOM:
            return f"custom {self.session_start_utc}-{self.session_end_utc} UTC"
        start, end = self.session_window()  # type: ignore[misc]
        return f"{self.session.value.replace('_', ' ')} {_format_minutes(start)}-{_format_minutes(end)} UTC"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "symbol": self.symbol,
            "start_utc": self.start_utc.isoformat().replace("+00:00", "Z"),
            "end_utc": self.end_utc.isoformat().replace("+00:00", "Z"),
            "target_r": self.target_r,
            "breakeven_r": self.breakeven_r,
            "profit_lock_trigger_r": self.profit_lock_trigger_r,
            "profit_lock_r": self.profit_lock_r,
            "risk_usd": self.risk_usd,
            "pivot_left": self.pivot_left,
            "pivot_right": self.pivot_right,
            "session": self.session.value,
            "session_start_utc": self.session_start_utc,
            "session_end_utc": self.session_end_utc,
        }


@dataclass(frozen=True)
class ReplayLifecycleEvent:
    timestamp: datetime
    event: str
    price: float | None = None
    stop: float | None = None
    target: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp.isoformat().replace("+00:00", "Z"),
            "event": self.event,
            "price": self.price,
            "stop": self.stop,
            "target": self.target,
        }


@dataclass
class ReplayTrade:
    trade_id: str
    date_utc: str
    symbol: str
    side: ReplaySide
    setup_id: str
    execution_tag: str
    trigger_proxy: str
    intent_time: datetime
    s1_time: datetime
    aoi_time: datetime
    s2_time: datetime
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    result: ReplayResultKind = ReplayResultKind.OPEN
    r_multiple: float | None = None
    pnl_usd: float | None = None
    active_stop: float | None = None
    breakeven_moved: bool = False
    profit_locked: bool = False
    lifecycle: list[ReplayLifecycleEvent] = field(default_factory=list)
    ex_price: float | None = None
    px_price: float | None = None
    ep_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        stamp = lambda value: value.isoformat().replace("+00:00", "Z") if value else None
        return {
            "trade_id": self.trade_id,
            "date": self.date_utc,
            "symbol": self.symbol,
            "pair": self.symbol,
            "side": self.side.value,
            "setup_id": self.setup_id,
            "execution_tag": self.execution_tag,
            "trigger_proxy": self.trigger_proxy,
            "intent_time_utc": stamp(self.intent_time),
            "s1_time_utc": stamp(self.s1_time),
            "aoi_time_utc": stamp(self.aoi_time),
            "s2_time_utc": stamp(self.s2_time),
            "entry_time_utc": stamp(self.entry_time),
            "exit_time_utc": stamp(self.exit_time),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "result": self.result.value,
            "r_multiple": self.r_multiple,
            "pnl_usd": self.pnl_usd,
            "active_stop": self.active_stop,
            "ex_price": self.ex_price,
            "px_price": self.px_price,
            "ep_price": self.ep_price,
            "lifecycle": [event.to_dict() for event in self.lifecycle],
        }


@dataclass(frozen=True)
class ReplayMetrics:
    """Derived outcome, streak, lifecycle, and exposure metrics for a run."""

    breakevens: int
    target_exits: int
    stop_exits: int
    stop_losses: int
    stop_breakevens: int
    stop_profit_locks: int
    break_even_moves: int
    profit_lock_moves: int
    win_rate: float
    loss_rate: float
    breakeven_rate: float
    avg_r: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    profit_factor: float | None
    max_win_streak: int
    max_loss_streak: int
    max_non_positive_streak: int
    current_streak: int
    current_streak_type: str
    max_drawdown_r: float
    max_drawdown_usd: float
    max_trades_per_day: int
    max_losses_per_day: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "breakevens": self.breakevens,
            "target_exits": self.target_exits,
            "stop_exits": self.stop_exits,
            "stop_losses": self.stop_losses,
            "stop_breakevens": self.stop_breakevens,
            "stop_profit_locks": self.stop_profit_locks,
            "break_even_moves": self.break_even_moves,
            "profit_lock_moves": self.profit_lock_moves,
            "win_rate": self.win_rate,
            "loss_rate": self.loss_rate,
            "breakeven_rate": self.breakeven_rate,
            "avg_r": self.avg_r,
            "avg_win_r": self.avg_win_r,
            "avg_loss_r": self.avg_loss_r,
            "expectancy_r": self.expectancy_r,
            "profit_factor": self.profit_factor,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "max_non_positive_streak": self.max_non_positive_streak,
            "current_streak": self.current_streak,
            "current_streak_type": self.current_streak_type,
            "max_drawdown_r": self.max_drawdown_r,
            "max_drawdown_usd": self.max_drawdown_usd,
            "max_trades_per_day": self.max_trades_per_day,
            "max_losses_per_day": self.max_losses_per_day,
        }


@dataclass(frozen=True)
class ReplaySummary:
    sessions: int
    trades: int
    wins: int
    losses: int
    open_trades: int
    total_r: float
    pnl_usd: float
    breakevens: int = 0
    metrics: ReplayMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "sessions": self.sessions,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "breakevens": self.breakevens,
            "open_trades": self.open_trades,
            "total_r": self.total_r,
            "pnl_usd": self.pnl_usd,
        }
        if self.metrics is not None:
            payload["metrics"] = self.metrics.to_dict()
        return payload


@dataclass(frozen=True)
class ReplayResult:
    request: ReplayRequest
    summary: ReplaySummary
    trades: list[ReplayTrade]
    cumulative: list[dict[str, Any]]
    bars: int
    run_id: str = ""
    skipped_signals: int = 0
    proxy_notice: str = (
        "Historical execution uses a completed 1-minute trigger proxy and does "
        "not claim exact tick or 5-second fills."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "summary": self.summary.to_dict(),
            "trades": [trade.to_dict() for trade in self.trades],
            "cumulative": self.cumulative,
            "bars": self.bars,
            "run_id": self.run_id,
            "skipped_signals": self.skipped_signals,
            "proxy_notice": self.proxy_notice,
        }
