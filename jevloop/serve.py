"""`jevloop serve`: a tiny static file server for the dashboard.

Serves the dashboard HTML files alongside ~/.jev-loop/latest.json so
dashboard/index.html and dashboard/wall.html can poll it with a plain
fetch(). No framework, no build step: http.server with two directories
merged via a symlink-free request handler.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import http.server
import json
import os
import socketserver
import threading
import time
from uuid import uuid4
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

from .execution.sweep import DEFAULT_EXECUTION_MODEL_TAG
from .blueprint_replay import (
    BLUEPRINT_VX_TAG,
    BlueprintRun,
    BlueprintRunStore,
    build_fixture_run,
    build_historical_run,
    canonical_model_tag,
)
from .historical_replay import HistoricalReplayError, replay_historical_range
from .replay import replay_range

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
SKILL_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = SKILL_DIR / "dashboard"
BLUEPRINT_RUNS = BlueprintRunStore()
BLUEPRINT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="blueprint-replay")
BLUEPRINT_JOBS: dict[str, dict] = {}
BLUEPRINT_JOBS_LOCK = threading.RLock()


def _job_snapshot(job: dict) -> dict:
    snapshot = {key: value for key, value in job.items() if key != "future"}
    snapshot["status"] = snapshot.get("state")
    heartbeat = snapshot.get("heartbeat")
    snapshot["updated_at_utc"] = datetime.fromtimestamp(heartbeat, tz=timezone.utc).isoformat().replace("+00:00", "Z") if heartbeat else None
    snapshot.setdefault("processed", 0)
    snapshot.setdefault("total", None)
    return snapshot


def _set_job(job_id: str, **changes) -> dict:
    with BLUEPRINT_JOBS_LOCK:
        job = BLUEPRINT_JOBS[job_id]
        job.update(changes)
        job["heartbeat"] = time.time()
        return _job_snapshot(job)


def _run_blueprint_job(job_id: str, payload: dict) -> None:
    _set_job(job_id, state="running", stage="replaying", progress=0.1, message="Replaying completed bars")
    try:
        start = date.fromisoformat(str(payload["start_utc"])[:10])
        end = date.fromisoformat(str(payload["end_utc"])[:10])
        tag = str(payload.get("execution_model_tag") or payload.get("tag") or BLUEPRINT_VX_TAG)
        canonical_model_tag(tag)
        source = str(payload.get("source", "fixture")).lower()
        if source == "fixture":
            run = build_fixture_run(start, end, tag)
        elif source in {"historical", "alpaca"}:
            run = build_historical_run(start, end, tag)
        else:
            raise ValueError("source must be historical or fixture")
        BLUEPRINT_RUNS.put(run)
        _set_job(job_id, state="complete", stage="complete", progress=1.0, message="Replay complete", run_id=run.run_id, result=run.result)
    except (HistoricalReplayError, ValueError, KeyError) as exc:
        _set_job(job_id, state="failed", stage="error", progress=1.0, message=str(exc), error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive boundary for worker errors
        _set_job(job_id, state="failed", stage="error", progress=1.0, message="Replay failed", error=str(exc))


def _new_blueprint_job(payload: dict) -> dict:
    job_id = uuid4().hex
    job = {
        "job_id": job_id,
        "state": "queued",
        "stage": "queued",
        "progress": 0.0,
        "message": "Replay queued",
        "heartbeat": time.time(),
        "run_id": None,
        "result": None,
        "error": None,
    }
    with BLUEPRINT_JOBS_LOCK:
        BLUEPRINT_JOBS[job_id] = job
    job["future"] = BLUEPRINT_EXECUTOR.submit(_run_blueprint_job, job_id, payload)
    return _job_snapshot(job)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/replay.json":
            self._serve_replay()
            return
        if path.startswith("/api/replay/jobs/"):
            self._serve_blueprint_job(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/replay/") and path.endswith("/chart"):
            self._serve_blueprint_chart()
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path != "/api/replay/jobs":
            self._json_response({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            job = _new_blueprint_job(payload)
            self._json_response({"ok": True, "job": job}, 202)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json_response({"ok": False, "error": str(exc)}, 400)

    def _json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_blueprint_job(self, job_id: str) -> None:
        with BLUEPRINT_JOBS_LOCK:
            job = BLUEPRINT_JOBS.get(job_id)
            payload = _job_snapshot(job) if job else None
        if payload is None:
            self._json_response({"ok": False, "error": "unknown replay job"}, 404)
            return
        self._json_response({"ok": True, "job": payload})

    def _serve_blueprint_chart(self) -> None:
        parts = [part for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 6 or parts[:2] != ["api", "replay"] or parts[3] != "trades" or parts[5] != "chart":
            self._json_response({"ok": False, "error": "invalid chart path"}, 404)
            return
        run = BLUEPRINT_RUNS.get(parts[2])
        if run is None:
            self._json_response({"ok": False, "error": "unknown replay run"}, 404)
            return
        query = parse_qs(urlsplit(self.path).query)
        timeframe = query.get("timeframe", ["1m"])[0]
        try:
            payload = run.chart(parts[4], timeframe)
        except (KeyError, ValueError) as exc:
            self._json_response({"ok": False, "error": str(exc)}, 400 if isinstance(exc, ValueError) else 404)
            return
        self._json_response(payload)

    def _serve_replay(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        start_text = query.get("start", [""])[0]
        end_text = query.get("end", [start_text])[0]
        tag = query.get("tag", [DEFAULT_EXECUTION_MODEL_TAG])[0].strip()
        source = query.get("source", ["historical"])[0].strip().lower()
        try:
            start = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text)
            if not tag:
                raise ValueError("tag must not be blank")
            if source == "fixture":
                payload = replay_range(start, end, tag)
            elif source in {"historical", "alpaca"}:
                payload = replay_historical_range(start, end, tag)
            else:
                raise ValueError("source must be historical or fixture")
            status = 200
        except HistoricalReplayError as exc:
            payload = {"error": str(exc), "source": source}
            status = 503
        except ValueError as exc:
            payload = {"error": str(exc)}
            status = 400
        self._json_response(payload, status)

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/latest.json", "/log.jsonl"):
            return str(LOG_DIR / path.lstrip("/"))
        if path == "/":
            path = "/index.html"
        candidate = DASHBOARD_DIR / path.lstrip("/")
        return str(candidate)

    def log_message(self, format, *args):  # noqa: A002
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop serve")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    # The CLI entry point already loads .env, but the dashboard is often
    # started by importing this module directly during local development.
    load_dotenv()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    latest = LOG_DIR / "latest.json"
    if not latest.exists():
        latest.write_text('{"ticks": [], "stats": {}}')

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"dashboard: http://127.0.0.1:{args.port}/index.html")
        print(f"replay lab: http://127.0.0.1:{args.port}/replay.html")
        print(f"Blueprint-VX replay: http://127.0.0.1:{args.port}/blueprint-vx-replay.html")
        print(f"dark wall: http://127.0.0.1:{args.port}/wall.html")
        print(f"raw feed:  http://127.0.0.1:{args.port}/latest.json")
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
