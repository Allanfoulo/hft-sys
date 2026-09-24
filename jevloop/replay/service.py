"""Request validation and provider selection for replay runs."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from threading import Lock, Thread
from uuid import uuid4

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

    def run(
        self,
        payload: dict,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> ReplayResult:
        if progress:
            progress({
                "stage": "validating",
                "processed": 0,
                "total": None,
                "current_time_utc": None,
                "message": "Validating replay range and symbol",
            })
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
            if progress:
                progress({
                    "stage": "fetching",
                    "processed": 0,
                    "total": None,
                    "current_time_utc": None,
                    "message": "Loading completed 1-minute historical bars",
                })
            bars = provider.get_bars(spec.symbol, request.start_utc, request.end_utc)
        except ReplayError:
            raise
        except Exception as exc:
            raise ReplayProviderError(f"historical data unavailable: {exc}") from exc
        if not bars:
            raise ReplayProviderError("no completed 1-minute bars were returned for this range")

        try:
            engine = ReplayEngine(request.pivot_left, request.pivot_right)
            result = engine.replay(bars, request, progress=progress)
            run_id = self._run_id(request, bars)
            result = replace(result, run_id=run_id)
            self._runs[run_id] = (result, tuple(bars))
            self._runs.move_to_end(run_id)
            while len(self._runs) > self._max_cached_runs:
                self._runs.popitem(last=False)
            if progress:
                progress({
                    "stage": "complete",
                    "processed": len(bars),
                    "total": len(bars),
                    "current_time_utc": bars[-1].timestamp.isoformat().replace("+00:00", "Z"),
                    "message": f"Replay complete · {len(bars):,} bars · {len(result.trades)} trades",
                })
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


class ReplayJobManager:
    """Run replay requests in background threads with observable heartbeats."""

    def __init__(self, service: ReplayService, max_jobs: int = 32):
        self.service = service
        self.max_jobs = max_jobs
        self._jobs: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._lock = Lock()

    def start(self, payload: dict) -> dict[str, object]:
        job_id = f"replay-job-{uuid4().hex[:12]}"
        now = self._now()
        job = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "message": "Replay queued",
            "processed": 0,
            "total": None,
            "progress": 0.0,
            "current_time_utc": None,
            "started_at_utc": None,
            "updated_at_utc": now,
            "finished_at_utc": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._prune()
            self._jobs[job_id] = job
        Thread(target=self._run, args=(job_id, payload), daemon=True).start()
        return self.status(job_id) or job.copy()

    def status(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.copy() if job else None

    def _run(self, job_id: str, payload: dict) -> None:
        self._update(
            job_id,
            status="running",
            stage="validating",
            started_at_utc=self._now(),
            message="Validating replay range and symbol",
        )

        def progress(event: dict[str, object]) -> None:
            total = event.get("total")
            processed = int(event.get("processed") or 0)
            fraction = round(processed / int(total), 4) if total else None
            self._update(job_id, **event, progress=fraction)

        try:
            result = self.service.run(payload, progress=progress)
        except ReplayError as exc:
            self._update(
                job_id,
                status="failed",
                stage="failed",
                message=str(exc),
                error=str(exc),
                finished_at_utc=self._now(),
            )
        except Exception as exc:  # pragma: no cover - final job safety net
            self._update(
                job_id,
                status="failed",
                stage="failed",
                message=f"replay server error: {exc}",
                error=f"replay server error: {exc}",
                finished_at_utc=self._now(),
            )
        else:
            self._update(
                job_id,
                status="complete",
                stage="complete",
                message=f"Replay complete · {result.bars:,} bars · {len(result.trades)} trades",
                processed=result.bars,
                total=result.bars,
                progress=1.0,
                result=result.to_dict(),
                finished_at_utc=self._now(),
            )

    def _update(self, job_id: str, **changes: object) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(changes)
            job["updated_at_utc"] = self._now()

    def _prune(self) -> None:
        while len(self._jobs) >= self.max_jobs:
            oldest_id, oldest = next(iter(self._jobs.items()))
            if oldest["status"] in {"queued", "running"}:
                break
            self._jobs.pop(oldest_id, None)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
