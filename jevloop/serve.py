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
from pathlib import Path
from urllib.parse import urlsplit

from .mt5_bridge import MT5BridgeService, MT5BridgeValidationError
from .replay.service import (
    ReplayError,
    ReplayNotFoundError,
    ReplayProviderError,
    ReplayJobManager,
    ReplayService,
    ReplayValidationError,
)

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
SKILL_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = SKILL_DIR / "dashboard"
REPLAY_SERVICE = ReplayService()
REPLAY_JOBS = ReplayJobManager(REPLAY_SERVICE)
MT5_BRIDGE = MT5BridgeService()
MT5_BRIDGE_TOKEN = os.environ.get("JEV_MT5_BRIDGE_TOKEN")


class Handler(http.server.SimpleHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parts = [part for part in urlsplit(self.path).path.split("/") if part]
        if parts == ["api", "mt5", "status"]:
            self._json(200, {"ok": True, "bridge": MT5_BRIDGE.status()})
            return
        if len(parts) == 4 and parts[:3] == ["api", "replay", "jobs"]:
            job = REPLAY_JOBS.status(parts[3])
            if job is None:
                self._json(404, {"ok": False, "error": "replay job was not found"})
            else:
                self._json(200, {"ok": True, "job": job})
            return
        if len(parts) == 6 and parts[:2] == ["api", "replay"] and parts[3] == "trades" and parts[5] == "chart":
            try:
                chart = REPLAY_SERVICE.trade_chart(parts[2], parts[4])
                self._json(200, {"ok": True, "chart": chart})
            except ReplayNotFoundError as exc:
                self._json(404, {"ok": False, "error": str(exc)})
            except ReplayValidationError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            except ReplayError as exc:
                self._json(422, {"ok": False, "error": str(exc)})
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/api/mt5/isx/decision":
            if MT5_BRIDGE_TOKEN and self.headers.get("X-Jev-Bridge-Token") != MT5_BRIDGE_TOKEN:
                self._json(401, {"ok": False, "error": "invalid MT5 bridge token"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise MT5BridgeValidationError("request body must be between 1 byte and 1 MB")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise MT5BridgeValidationError("request body must be a JSON object")
                self._json(200, MT5_BRIDGE.decide(payload))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._json(400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            except MT5BridgeValidationError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # pragma: no cover - final HTTP safety net
                self._json(500, {"ok": False, "error": f"MT5 bridge error: {exc}"})
            return
        if route == "/api/replay/jobs":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ReplayValidationError("request body must be between 1 byte and 1 MB")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                job = REPLAY_JOBS.start(payload)
                self._json(202, {"ok": True, "job": job})
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._json(400, {"ok": False, "error": f"invalid JSON body: {exc}"})
            except ReplayValidationError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if route != "/api/replay":
            self._json(404, {"error": "unknown API route"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ReplayValidationError("request body must be between 1 byte and 1 MB")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = REPLAY_SERVICE.run(payload)
            self._json(200, {"ok": True, "result": result.to_dict()})
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"invalid JSON body: {exc}"})
        except ReplayValidationError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except ReplayProviderError as exc:
            self._json(502, {"ok": False, "error": str(exc)})
        except ReplayError as exc:
            self._json(422, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - final HTTP safety net
            self._json(500, {"ok": False, "error": f"replay server error: {exc}"})

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

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    latest = LOG_DIR / "latest.json"
    if not latest.exists():
        latest.write_text('{"ticks": [], "stats": {}}')

    class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with ThreadingTCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"dashboard: http://127.0.0.1:{args.port}/index.html")
        print(f"ISX replay: http://127.0.0.1:{args.port}/isx-replay.html")
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
