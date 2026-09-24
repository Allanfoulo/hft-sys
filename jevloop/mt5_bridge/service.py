"""A read-only HTTP decision bridge for MetaTrader 5.

The bridge accepts completed bars supplied by an MT5 Expert Advisor and
returns a deterministic ISX decision. It never submits orders and therefore
keeps the Python side safe even when the EA is configured for shadow mode.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Mapping, Sequence

from ..isx.engine import ISXEngine
from ..isx.models import Candle, Direction, ISXDecision


class MT5BridgeValidationError(ValueError):
    """Raised when an EA payload is malformed or unsafe to evaluate."""


@dataclass
class _Session:
    symbol: str
    engine: ISXEngine
    created_at_utc: str
    last_seen_utc: str
    requests: int = 0
    last_closed_candle_utc: str | None = None


@dataclass
class MT5BridgeService:
    """Keep one idempotent ISX engine per MT5 symbol/account session."""

    pivot_left: int = 2
    pivot_right: int = 2
    max_sessions: int = 64
    max_bars_per_timeframe: int = 1_000
    _sessions: OrderedDict[str, _Session] = field(default_factory=OrderedDict)
    _lock: Lock = field(default_factory=Lock)

    def decide(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        session_id = self._text(payload, "session_id", 128)
        symbol = self._text(payload, "symbol", 32).upper()
        bars = self._parse_bars(payload.get("bars"))
        trigger = self._parse_trigger(payload.get("trigger"))
        now = self._now()

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.symbol != symbol or payload.get("reset"):
                session = _Session(
                    symbol=symbol,
                    engine=ISXEngine(self.pivot_left, self.pivot_right),
                    created_at_utc=now,
                    last_seen_utc=now,
                )
                self._sessions[session_id] = session
            session.last_seen_utc = now
            session.requests += 1
            self._sessions.move_to_end(session_id)
            self._prune()
            decision = session.engine.update(bars)
            session.last_closed_candle_utc = self._last_closed(bars)

        return self._response(session_id, symbol, session, decision, trigger, payload)

    def status(self) -> dict[str, Any]:
        with self._lock:
            sessions = [
                {
                    "session_id": session_id,
                    "symbol": session.symbol,
                    "created_at_utc": session.created_at_utc,
                    "last_seen_utc": session.last_seen_utc,
                    "last_closed_candle_utc": session.last_closed_candle_utc,
                    "requests": session.requests,
                    "state": session.engine.state.observability(),
                }
                for session_id, session in self._sessions.items()
            ]
        return {"sessions": sessions, "session_count": len(sessions)}

    def _response(
        self,
        session_id: str,
        symbol: str,
        session: _Session,
        decision: ISXDecision,
        trigger: dict[str, float | str | None],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        direction = decision.direction
        entry = None
        stop = decision.observability.get("invalidation_price")
        target = None
        side = None
        target_r = self._number(payload.get("target_r", 4.0), "target_r", 0.1, 20.0)
        if direction is Direction.BULLISH:
            side = "BUY"
            entry = trigger.get("ask")
        elif direction is Direction.BEARISH:
            side = "SELL"
            entry = trigger.get("bid")
        if decision.x_ready and isinstance(entry, (float, int)) and isinstance(stop, (float, int)):
            risk_distance = abs(float(entry) - float(stop))
            if risk_distance > 0 and ((direction is Direction.BULLISH and stop < entry) or (direction is Direction.BEARISH and stop > entry)):
                target = float(entry) + (risk_distance * target_r if direction is Direction.BULLISH else -risk_distance * target_r)
            else:
                decision = self._stand_down(decision, "MT5 trigger is on the invalid side of EX")

        execution_allowed = decision.x_ready and entry is not None and stop is not None and target is not None
        return {
            "ok": True,
            "protocol_version": 1,
            "model": "isx-python-hybrid-v1",
            "session_id": session_id,
            "symbol": symbol,
            "heartbeat_at_utc": self._now(),
            "live_execution_allowed": False,
            "decision": {
                "action": decision.action,
                "direction": decision.direction.value,
                "phase": decision.phase.value,
                "setup_id": decision.setup_id,
                "x_ready": decision.x_ready,
                "execution_tag": decision.execution_tag,
                "reason": decision.reason,
                "observability": decision.observability,
            },
            "execution": {
                "allowed": execution_allowed,
                "side": side,
                "entry": entry,
                "stop": stop,
                "target": target,
                "target_r": target_r,
                "trigger_proxy": "mt5-bid-ask-at-poll",
            },
            "bridge": {
                "session_requests": session.requests,
                "last_closed_candle_utc": session.last_closed_candle_utc,
                "shadow_only": True,
            },
        }

    @staticmethod
    def _stand_down(decision: ISXDecision, reason: str) -> ISXDecision:
        return ISXDecision(
            phase=decision.phase,
            direction=decision.direction,
            action="STAND_DOWN",
            setup_id=decision.setup_id,
            x_ready=False,
            execution_tag=None,
            reason=reason,
            observability={**decision.observability, "stand_down_reason": reason, "x_ready": False},
        )

    def _parse_bars(self, value: Any) -> dict[str, list[Candle]]:
        if not isinstance(value, Mapping):
            raise MT5BridgeValidationError("bars must be an object containing H4, H1, and M15 arrays")
        parsed: dict[str, list[Candle]] = {}
        for timeframe in ("H4", "H1", "M15"):
            raw = value.get(timeframe)
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise MT5BridgeValidationError(f"bars.{timeframe} must be an array")
            if len(raw) > self.max_bars_per_timeframe:
                raise MT5BridgeValidationError(f"bars.{timeframe} exceeds {self.max_bars_per_timeframe} candles")
            parsed[timeframe] = [self._candle(item, timeframe) for item in raw]
        return parsed

    @classmethod
    def _candle(cls, value: Any, timeframe: str) -> Candle:
        if not isinstance(value, Mapping):
            raise MT5BridgeValidationError(f"bars.{timeframe} entries must be objects")
        timestamp_value = value.get("timestamp_utc", value.get("time"))
        if timestamp_value is None:
            raise MT5BridgeValidationError(f"bars.{timeframe} entry is missing timestamp_utc")
        timestamp = cls._timestamp(timestamp_value)
        try:
            open_price = float(value["open"])
            high = float(value["high"])
            low = float(value["low"])
            close = float(value["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MT5BridgeValidationError(f"bars.{timeframe} entry has invalid OHLC values") from exc
        if min(open_price, high, low, close) <= 0 or high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise MT5BridgeValidationError(f"bars.{timeframe} entry has inconsistent OHLC values")
        return Candle(timestamp, open_price, high, low, close, bool(value.get("complete", True)))

    @classmethod
    def _parse_trigger(cls, value: Any) -> dict[str, float | str | None]:
        if value is None:
            return {"timestamp_utc": None, "bid": None, "ask": None}
        if not isinstance(value, Mapping):
            raise MT5BridgeValidationError("trigger must be an object")
        bid = cls._optional_number(value.get("bid"), "trigger.bid")
        ask = cls._optional_number(value.get("ask"), "trigger.ask")
        if (bid is not None and bid <= 0) or (ask is not None and ask <= 0) or (bid is not None and ask is not None and ask < bid):
            raise MT5BridgeValidationError("trigger bid/ask must be positive with ask >= bid")
        timestamp = value.get("timestamp_utc", value.get("time"))
        return {"timestamp_utc": cls._timestamp(timestamp) if timestamp is not None else None, "bid": bid, "ask": ask}

    @staticmethod
    def _last_closed(bars: Mapping[str, Sequence[Candle]]) -> str | None:
        timestamps = [c.timestamp for series in bars.values() for c in series if c.complete]
        return max(timestamps).isoformat().replace("+00:00", "Z") if timestamps else None

    def _prune(self) -> None:
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        try:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(float(value), timezone.utc)
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise MT5BridgeValidationError("timestamps must be ISO-8601 UTC or Unix seconds") from exc
        if parsed.tzinfo is None:
            raise MT5BridgeValidationError("timestamps must include a UTC offset")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _text(payload: Mapping[str, Any], key: str, max_length: int) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > max_length:
            raise MT5BridgeValidationError(f"{key} must be a non-empty string of at most {max_length} characters")
        return value.strip()

    @classmethod
    def _number(cls, value: Any, key: str, minimum: float, maximum: float) -> float:
        number = cls._optional_number(value, key)
        if number is None or number < minimum or number > maximum:
            raise MT5BridgeValidationError(f"{key} must be between {minimum} and {maximum}")
        return number

    @staticmethod
    def _optional_number(value: Any, key: str) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MT5BridgeValidationError(f"{key} must be numeric") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise MT5BridgeValidationError(f"{key} must be finite")
        return number

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
