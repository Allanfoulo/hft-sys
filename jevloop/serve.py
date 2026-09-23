"""`jevloop serve`: a tiny static file server for the dashboard.

Serves the dashboard HTML files alongside ~/.jev-loop/latest.json so
dashboard/index.html and dashboard/wall.html can poll it with a plain
fetch(). No framework, no build step: http.server with two directories
merged via a symlink-free request handler.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

from .execution.sweep import DEFAULT_EXECUTION_MODEL_TAG
from .historical_replay import HistoricalReplayError, replay_historical_range
from .replay import replay_range

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
SKILL_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = SKILL_DIR / "dashboard"


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if urlsplit(self.path).path == "/replay.json":
            self._serve_replay()
            return
        super().do_GET()

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
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
