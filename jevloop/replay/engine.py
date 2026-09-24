"""Deterministic, read-only ISX replay engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from ..isx.models import Candle, Direction, ISXPhase, RangeAnchors
from ..isx.structure import (
    completed_candles,
    detect_breaks,
    detect_pivots,
    latest_intent_break,
    range_anchors,
    structure_direction,
)
from .models import (
    ReplayLifecycleEvent,
    ReplayRequest,
    ReplayResult,
    ReplayResultKind,
    ReplaySide,
    ReplaySummary,
    ReplayTrade,
)


@dataclass
class _Setup:
    setup_id: str
    direction: Direction
    intent_time: datetime
    anchors: RangeAnchors
    phase: ISXPhase = ISXPhase.WAIT_S1
    s1_time: datetime | None = None
    aoi_time: datetime | None = None
    s2_time: datetime | None = None
    entry_index: int | None = None
    invalidated: bool = False


class ReplayEngine:
    """Replay ISX structure using completed historical bars only.

    The engine never receives an Alpaca client and has no order side effects.
    Each prefix of the minute series is evaluated before a transition is
    accepted, which prevents future-bar leakage.
    """

    def __init__(self, pivot_left: int = 1, pivot_right: int = 1):
        self.pivot_left = pivot_left
        self.pivot_right = pivot_right

    def replay(self, minute_bars: Sequence[Candle], request: ReplayRequest) -> ReplayResult:
        bars = [
            bar
            for bar in completed_candles(minute_bars)
            if request.start_utc <= bar.timestamp < request.end_utc
        ]
        all_fifteen = aggregate_15m(bars)
        trades: list[ReplayTrade] = []
        setup: _Setup | None = None
        open_trade: ReplayTrade | None = None
        trade_number = 0
        fifteen_cursor = 0

        for index, bar in enumerate(bars):
            if open_trade is not None:
                if index > self._entry_index(open_trade, bars):
                    if self._advance_trade(open_trade, bar, request):
                        trades.append(open_trade)
                        open_trade = None
                continue

            while (
                fifteen_cursor < len(all_fifteen)
                and all_fifteen[fifteen_cursor].timestamp + timedelta(minutes=14)
                <= bar.timestamp
            ):
                fifteen_cursor += 1
                fifteen = all_fifteen[:fifteen_cursor]
                intent_direction = structure_direction(
                    fifteen, self.pivot_left, self.pivot_right
                )
                intent_break = (
                    latest_intent_break(
                        fifteen,
                        intent_direction,
                        self.pivot_left,
                        self.pivot_right,
                    )
                    if intent_direction is not Direction.NEUTRAL
                    else None
                )
                anchors = (
                    range_anchors(
                        fifteen,
                        intent_break,
                        intent_direction,
                        self.pivot_left,
                        self.pivot_right,
                    )
                    if intent_break
                    else None
                )
                if intent_break and anchors and (
                    setup is None or intent_break.timestamp > setup.intent_time
                ):
                    setup = _Setup(
                        self._setup_id(intent_direction, intent_break.timestamp),
                        intent_direction,
                        intent_break.timestamp,
                        anchors,
                    )

            minute_prefix = bars[: index + 1]
            if setup is None or setup.invalidated or setup.phase is ISXPhase.EXECUTED:
                continue

            if setup.s1_time is None:
                s1 = self._first_shift(
                    minute_prefix, setup.intent_time, setup.direction, opposite=True
                )
                if s1:
                    setup.s1_time = s1.timestamp
                    setup.phase = ISXPhase.WAIT_AOI

            if setup.s1_time is None:
                continue

            if self._invalidated(setup, bar.close):
                setup.invalidated = True
                setup.phase = ISXPhase.INVALIDATED
                continue

            if setup.aoi_time is None:
                for candidate in minute_prefix:
                    if candidate.timestamp <= setup.s1_time:
                        continue
                    fib = self._fib(setup, candidate.close)
                    if 0.618 <= fib <= 0.790:
                        setup.aoi_time = candidate.timestamp
                        setup.phase = ISXPhase.WAIT_S2
                        break

            if setup.aoi_time is None:
                continue

            if setup.s2_time is None:
                s2 = self._first_shift(
                    minute_prefix, setup.aoi_time, setup.direction, opposite=False
                )
                if s2:
                    setup.s2_time = s2.timestamp
                    setup.entry_index = s2.index + 1
                    setup.phase = ISXPhase.X_READY

            if setup.entry_index != index:
                continue
            if setup.s2_time is None or setup.entry_index is None:
                continue

            trade_number += 1
            open_trade = self._open_trade(
                request,
                setup,
                bar,
                trade_number,
            )
            if open_trade is None:
                setup.invalidated = True
                setup.phase = ISXPhase.INVALIDATED
                continue
            setup.phase = ISXPhase.EXECUTED

        if open_trade is not None:
            trades.append(open_trade)

        return self._result(request, bars, trades)

    def _first_shift(self, bars, after, intent, opposite):
        direction = intent
        if opposite:
            direction = Direction.BEARISH if intent is Direction.BULLISH else Direction.BULLISH
        pivots = detect_pivots(bars, self.pivot_left, self.pivot_right)
        breaks = detect_breaks(bars, pivots, intent)
        matches = [b for b in breaks if b.timestamp > after and b.direction is direction]
        return matches[0] if matches else None

    @staticmethod
    def _fib(setup: _Setup, close: float) -> float:
        ex, ep = setup.anchors.ex, setup.anchors.ep
        if ex == ep:
            return 0.0
        if setup.direction is Direction.BULLISH:
            return (ep - close) / (ep - ex)
        return (close - ep) / (ex - ep)

    @staticmethod
    def _invalidated(setup: _Setup, close: float) -> bool:
        if setup.direction is Direction.BULLISH:
            return close <= setup.anchors.ex
        return close >= setup.anchors.ex

    def _open_trade(
        self,
        request: ReplayRequest,
        setup: _Setup,
        bar: Candle,
        number: int,
    ) -> ReplayTrade | None:
        side = ReplaySide.BUY if setup.direction is Direction.BULLISH else ReplaySide.SELL
        stop = setup.anchors.ex
        entry = bar.open
        risk = entry - stop if side is ReplaySide.BUY else stop - entry
        if risk <= 0:
            return None
        target = entry + request.target_r * risk if side is ReplaySide.BUY else entry - request.target_r * risk
        tag = f"ISX-REPLAY-{request.symbol.replace('/', '')}-{setup.intent_time.strftime('%Y%m%dT%H%M%S')}-{number:04d}"
        trade = ReplayTrade(
            trade_id=f"{request.symbol.replace('/', '')}-{number:04d}",
            date_utc=bar.timestamp.date().isoformat(),
            symbol=request.symbol,
            side=side,
            setup_id=setup.setup_id,
            execution_tag=tag,
            trigger_proxy="1m-trigger-proxy",
            intent_time=setup.intent_time,
            s1_time=setup.s1_time,
            aoi_time=setup.aoi_time,
            s2_time=setup.s2_time,
            entry_time=bar.timestamp,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            active_stop=stop,
            ex_price=setup.anchors.ex,
            px_price=setup.anchors.px,
            ep_price=setup.anchors.ep,
        )
        trade.lifecycle.append(
            ReplayLifecycleEvent(bar.timestamp, "ENTRY", entry, stop, target)
        )
        return trade

    @staticmethod
    def _entry_index(trade: ReplayTrade, bars: Sequence[Candle]) -> int:
        for index, bar in enumerate(bars):
            if bar.timestamp == trade.entry_time:
                return index
        return len(bars)

    def _advance_trade(self, trade: ReplayTrade, bar: Candle, request: ReplayRequest) -> bool:
        assert trade.active_stop is not None
        risk = abs(trade.entry_price - trade.stop_price)
        long = trade.side is ReplaySide.BUY
        stop_hit = bar.low <= trade.active_stop if long else bar.high >= trade.active_stop
        target_hit = bar.high >= trade.target_price if long else bar.low <= trade.target_price
        if stop_hit:
            self._close(trade, bar, trade.active_stop, ReplayResultKind.STOP, risk, request)
            return True
        if target_hit:
            self._close(trade, bar, trade.target_price, ReplayResultKind.TARGET, risk, request)
            return True

        favorable = (bar.high - trade.entry_price) / risk if long else (trade.entry_price - bar.low) / risk
        if favorable >= request.breakeven_r and not trade.breakeven_moved:
            trade.active_stop = trade.entry_price
            trade.breakeven_moved = True
            trade.lifecycle.append(ReplayLifecycleEvent(bar.timestamp, "BREAK_EVEN", trade.entry_price, trade.active_stop, trade.target_price))
        if favorable >= request.profit_lock_trigger_r and not trade.profit_locked:
            locked = trade.entry_price + request.profit_lock_r * risk if long else trade.entry_price - request.profit_lock_r * risk
            improves = locked > trade.active_stop if long else locked < trade.active_stop
            trade.profit_locked = True
            if improves:
                trade.active_stop = locked
                trade.lifecycle.append(ReplayLifecycleEvent(bar.timestamp, "PROFIT_LOCK", locked, trade.active_stop, trade.target_price))
        return False

    @staticmethod
    def _close(trade, bar, price, result, risk, request):
        trade.exit_time = bar.timestamp
        trade.exit_price = price
        trade.result = result
        trade.r_multiple = ((price - trade.entry_price) / risk) if trade.side is ReplaySide.BUY else ((trade.entry_price - price) / risk)
        trade.pnl_usd = trade.r_multiple * request.risk_usd
        trade.lifecycle.append(ReplayLifecycleEvent(bar.timestamp, result.value, price, trade.active_stop, trade.target_price))

    @staticmethod
    def _setup_id(direction, timestamp):
        return f"ISX-{direction.value[:1].upper()}-{timestamp.strftime('%Y%m%dT%H%M%S')}"

    @staticmethod
    def _result(request, bars, trades):
        closed = [trade for trade in trades if trade.r_multiple is not None]
        wins = sum(1 for trade in closed if trade.r_multiple and trade.r_multiple > 0)
        losses = sum(1 for trade in closed if trade.r_multiple is not None and trade.r_multiple <= 0)
        total_r = sum(trade.r_multiple or 0.0 for trade in closed)
        pnl = sum(trade.pnl_usd or 0.0 for trade in closed)
        cumulative = []
        running_r = 0.0
        running_pnl = 0.0
        for trade in trades:
            if trade.r_multiple is None:
                continue
            running_r += trade.r_multiple
            running_pnl += trade.pnl_usd or 0.0
            cumulative.append({
                "timestamp_utc": (trade.exit_time or trade.entry_time).isoformat().replace("+00:00", "Z"),
                "trade_id": trade.trade_id,
                "execution_tag": trade.execution_tag,
                "r": running_r,
                "pnl_usd": running_pnl,
            })
        sessions = len({bar.timestamp.date().isoformat() for bar in bars})
        summary = ReplaySummary(sessions, len(trades), wins, losses, len(trades) - len(closed), total_r, pnl)
        return ReplayResult(request, summary, trades, cumulative, len(bars))


def aggregate_15m(minute_bars: Sequence[Candle]) -> list[Candle]:
    """Aggregate only complete, contiguous UTC 15-minute buckets."""
    grouped: dict[datetime, list[Candle]] = {}
    for bar in completed_candles(minute_bars):
        stamp = bar.timestamp.astimezone(timezone.utc)
        bucket = stamp.replace(minute=(stamp.minute // 15) * 15, second=0, microsecond=0)
        grouped.setdefault(bucket, []).append(bar)
    result: list[Candle] = []
    for bucket, group in sorted(grouped.items()):
        group = sorted(group, key=lambda item: item.timestamp)
        if len(group) != 15:
            continue
        if any(group[i].timestamp - group[i - 1].timestamp != timedelta(minutes=1) for i in range(1, len(group))):
            continue
        result.append(Candle(bucket, group[0].open, max(item.high for item in group), min(item.low for item in group), group[-1].close, True))
    return result
