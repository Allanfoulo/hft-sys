"""Request validation and provider selection for replay runs."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from hashlib import sha256
import json

from ..assets import UnknownSymbolError, resolve_symbol
from ..execution.alpaca import client_from_env
from ..isx.models import Candle
from .chart import build_trade_chart
from .data import AlpacaMinuteBarsProvider, FixtureMinuteBarsProvider, MinuteBarsProvider
from .engine import ReplayEngine
from .models import ReplayRequest, ReplayResult, ReplaySource


class ReplayError(Exception):
    """Base class for user-facing replay failures."""


class ReplayValidationError(ReplayError):
    pass


class ReplayProviderError(ReplayError):
    pass


class ReplayNotFoundError(ReplayError):
    pass


class ReplayService:
    """Run replay requests while keeping all order execution out of scope."""

    def __init__(
        self,
        fixture_provider: MinuteBarsProvider | None = None,
        client_factory: Callable[..., object] | None = None,
    ):
        self.fixture_provider = fixture_provider or FixtureMinuteBarsProvider()
        self.client_factory = client_factory or client_from_env
        self._runs: OrderedDict[str, tuple[ReplayResult, tuple[Candle, ...]]] = OrderedDict()
        self._max_cached_runs = 8

    def run(self, payload: dict) -> ReplayResult:
        try:
            request = ReplayRequest.from_payload(payload)
        except ValueError as exc:
            raise ReplayValidationError(str(exc)) from exc
        try:
            spec = resolve_symbol(request.symbol)
        except UnknownSymbolError as exc:
            raise ReplayValidationError(str(exc)) from exc
        if not spec.is_24_7:
            raise ReplayValidationError("ISX replay currently supports crypto pairs only")

        try:
            if request.source is ReplaySource.FIXTURE:
                provider = self.fixture_provider
            else:
                client = self.client_factory(symbol=spec.symbol, live=False)
                provider = AlpacaMinuteBarsProvider(client)
            bars = provider.get_bars(spec.symbol, request.start_utc, request.end_utc)
        except ReplayError:
            raise
        except Exception as exc:
            raise ReplayProviderError(f"historical data unavailable: {exc}") from exc
        if not bars:
            raise ReplayProviderError("no completed 1-minute bars were returned for this range")

        try:
            engine = ReplayEngine(request.pivot_left, request.pivot_right)
            result = engine.replay(bars, request)
            run_id = self._run_id(request, bars)
            result = replace(result, run_id=run_id)
            self._runs[run_id] = (result, tuple(bars))
            self._runs.move_to_end(run_id)
            while len(self._runs) > self._max_cached_runs:
                self._runs.popitem(last=False)
            return result
        except Exception as exc:
            raise ReplayError(f"replay failed: {exc}") from exc

    def trade_chart(self, run_id: str, trade_id: str) -> dict:
        cached = self._runs.get(run_id)
        if cached is None:
            raise ReplayNotFoundError("replay run has expired; run the replay again")
        result, bars = cached
        self._runs.move_to_end(run_id)
        trade = next((item for item in result.trades if item.trade_id == trade_id), None)
        if trade is None:
            raise ReplayNotFoundError("trade was not found in this replay run")
        return build_trade_chart(trade, bars, result.request)

    @staticmethod
    def _run_id(request: ReplayRequest, bars: Sequence[Candle]) -> str:
        digest = sha256(json.dumps(request.to_dict(), sort_keys=True).encode("utf-8"))
        for bar in bars:
            digest.update(
                f"{bar.timestamp.isoformat()}|{bar.open:.12g}|{bar.high:.12g}|{bar.low:.12g}|{bar.close:.12g};".encode(
                    "utf-8"
                )
            )
        return f"isx-replay-{digest.hexdigest()[:20]}"
