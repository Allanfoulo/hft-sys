"""Request validation and provider selection for replay runs."""

from __future__ import annotations

from collections.abc import Callable

from ..assets import UnknownSymbolError, resolve_symbol
from ..execution.alpaca import client_from_env
from .data import AlpacaMinuteBarsProvider, FixtureMinuteBarsProvider, MinuteBarsProvider
from .engine import ReplayEngine
from .models import ReplayRequest, ReplayResult, ReplaySource


class ReplayError(Exception):
    """Base class for user-facing replay failures."""


class ReplayValidationError(ReplayError):
    pass


class ReplayProviderError(ReplayError):
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
            return engine.replay(bars, request)
        except Exception as exc:
            raise ReplayError(f"replay failed: {exc}") from exc
