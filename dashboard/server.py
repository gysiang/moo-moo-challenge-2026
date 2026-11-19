from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from dashboard import cloud as cloud_api
from dashboard import guard as guard_link
from dashboard import state

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 8 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class App:
    def __init__(
        self,
        *,
        runtime: Path,
        config_path: Path,
        baseline: float,
        demo: bool,
        cloud: Optional[cloud_api.CloudClient] = None,
        guard: Optional[guard_link.GuardLink] = None,
        sync_status: Optional[dict] = None,
        sync_run: Optional[Any] = None,
    ) -> None:
        self.runtime = Path(runtime)
        self.config_path = Path(config_path)
        self.baseline = float(baseline)
        self.demo = demo
        self.cloud = cloud
        self.guard = guard
        self.sync_status = sync_status
        self.sync_run = sync_run
        self.approvals_file = "approvals-demo.json" if demo else "approvals.json"
        self.events_file = "guard-events-demo.jsonl" if demo else "guard-events.jsonl"
        self.history_file = "equity-history-demo.jsonl" if demo else "equity-history.jsonl"
        self.rejections_file = "rejections-demo.jsonl" if demo else "rejections.jsonl"
        self.triggers_file = "guard-triggers-demo.jsonl" if demo else "guard-triggers.jsonl"

    def state(self) -> dict:
        payload = state.build_state(
            runtime=self.runtime,
            config_path=self.config_path,
            baseline=self.baseline,
            demo=self.demo,
            approvals_file=self.approvals_file,
            events_file=self.events_file,
            history_file=self.history_file,
            rejections_file=self.rejections_file,
            triggers_file=self.triggers_file,
        )
        payload["guard_link"] = {
            "configured": self.guard is not None,
            "session_id": self.guard.session_id if self.guard else None,
        }
        return payload

    def approve(self, payload: dict[str, Any]) -> tuple[int, dict]:
        ticket_id = payload.get("ticket_id")
        cycle_file = payload.get("cycle_file")
        if not isinstance(ticket_id, str) or not isinstance(cycle_file, str):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "ticket_id and cycle_file are required"}
        ttl = payload.get("ttl_minutes")
        try:
            ttl_minutes = int(ttl) if ttl is not None else state.load_limits(self.config_path)["approval_ttl_minutes"]
        except (TypeError, ValueError):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "ttl_minutes must be an integer"}
        if not 1 <= ttl_minutes <= 480:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "ttl_minutes must be between 1 and 480"}
        result = state.approve_ticket(
            runtime=self.runtime,
            cycle_file=cycle_file,
            ticket_id=ticket_id,
            ttl_minutes=ttl_minutes,
            approvals_file=self.approvals_file,
            rejections_file=self.rejections_file,
        )
        if not result.get("ok"):
            return HTTPStatus.CONFLICT, result
        result["guard"] = self.trigger_guard(cycle_file, ticket_id)
        return HTTPStatus.OK, result

    def reject(self, payload: dict[str, Any]) -> tuple[int, dict]:
        ticket_id = payload.get("ticket_id")
        cycle_file = payload.get("cycle_file")
        if not isinstance(ticket_id, str) or not isinstance(cycle_file, str):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "ticket_id and cycle_file are required"}
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "reason must be text"}
        result = state.reject_ticket(
            runtime=self.runtime,
            cycle_file=cycle_file,
            ticket_id=ticket_id,
            reason=reason or "",
            approvals_file=self.approvals_file,
            rejections_file=self.rejections_file,
        )
        return (HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT), result

    def trigger_guard(self, cycle_file: str, ticket_id: str) -> dict:
        if self.demo:
            return {
                "configured": False,
                "triggered": False,
                "error": "demo fixtures never start the Guard; approvals here are synthetic",
            }
        if self.guard is None:
            return {"configured": False, "triggered": False, "error": "guard session not configured"}
        previous = state.load_triggers(self.runtime / self.triggers_file).get(ticket_id)
        if previous and previous.get("state") != "rejected":
            return {
                "configured": True,
                "triggered": False,
                "session_id": previous.get("session_id"),
                "error": f"a trigger for {ticket_id} already exists ({previous.get('state')}, {previous.get('requested_at')}); "
                "check the Guard session instead of sending another",
            }
        try:
            envelope = guard_link.build_envelope(
                runtime=self.runtime,
                cycle_file=cycle_file,
                ticket_id=ticket_id,
                approvals_file=self.approvals_file,
            )
        except guard_link.GuardLinkError as err:
            return {"configured": True, "triggered": False, "error": str(err)}
        base = {
            "ticket_id": ticket_id,
            "cycle_file": Path(cycle_file).name,
            "proposal_hash": envelope["evidence"]["approvals"][ticket_id]["proposal_hash"],
            "session_id": self.guard.session_id,
            "requested_at": envelope["requested_at"],
        }
        state.append_jsonl(self.runtime / self.triggers_file, {**base, "state": "sending"})
        outcome = self.guard.trigger(envelope)
        if outcome.get("triggered"):
            state.append_jsonl(self.runtime / self.triggers_file, {**base, "state": "accepted"})
            return {"configured": True, **outcome}
        # An HTTP response means the send was refused; no timeout means it may have landed.
        status = outcome.get("status", 0)
        final = "rejected" if status else "unknown"
        state.append_jsonl(
            self.runtime / self.triggers_file,
            {**base, "state": final, "error": outcome.get("error")},
        )
        return {"configured": True, **outcome, "state": final}

    def cloud_sessions(self, limit: int) -> dict:
        if self.cloud is None:
            return {"configured": False, "sessions": []}
        try:
            rows = self.cloud.sessions(limit=limit)
        except cloud_api.CloudError as err:
            return {"configured": True, "error": err.message, "sessions": []}
        return {"configured": True, "sessions": [cloud_api.summarize_session(r) for r in rows]}

    def cloud_events(self, session_id: str, limit: int) -> dict:
        if not cloud_api.SESSION_ID.match(session_id or ""):
            return {"configured": True, "error": "invalid session id", "events": []}
        if self.cloud is None:
            return {"configured": False, "events": []}
        try:
            rows = self.cloud.session_events(session_id, limit=limit)
        except cloud_api.CloudError as err:
            return {"configured": True, "error": err.message, "events": []}
        return {"configured": True, "events": cloud_api.summarize_events(rows)}

    def sync_state(self) -> dict:
        status = self.sync_status or {}
        return {
            "configured": self.sync_run is not None,
            "interval": status.get("interval"),
            "last": status.get("last"),
        }

    def sync_now(self) -> dict:
        if self.sync_run is None:
            return {"configured": False, "error": "artifact sync is not configured"}
        try:
            result = self.sync_run()
        except Exception as err:
            return {"configured": True, "error": str(err)[:200]}
        return {"configured": True, "result": result}


class Handler(BaseHTTPRequestHandler):
    server_version = "moomoo-dashboard/1"
    app: App

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _loopback(self) -> bool:
        host = (self.headers.get("Host") or "").split(":", 1)[0]
        if host not in LOOPBACK_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        return True

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._loopback():
            self._json(HTTPStatus.FORBIDDEN, {"error": "loopback only"})
            return
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._file(STATIC / "index.html")
            return
        if route == "/api/state":
            self._json(HTTPStatus.OK, self.app.state())
            return
        if route == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "demo": self.app.demo})
            return
        if route == "/api/cloud/sessions":
            try:
                limit = int((parse_qs(urlparse(self.path).query).get("limit") or ["12"])[0])
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
                return
            self._json(HTTPStatus.OK, self.app.cloud_sessions(limit))
            return
        if route == "/api/cloud/events":
            params = parse_qs(urlparse(self.path).query)
            session_id = (params.get("id") or [""])[0]
            if not cloud_api.SESSION_ID.match(session_id):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid session id"})
                return
            try:
                limit = int((params.get("limit") or ["40"])[0])
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
                return
            self._json(HTTPStatus.OK, self.app.cloud_events(session_id, limit))
            return
        if route == "/api/sync":
            self._json(HTTPStatus.OK, self.app.sync_state())
            return
        if route.startswith("/static/"):
            candidate = (STATIC / route[len("/static/"):]).resolve()
            if STATIC not in candidate.parents and candidate != STATIC:
                self._json(HTTPStatus.FORBIDDEN, {"error": "outside static"})
                return
            self._file(candidate)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._loopback():
            self._json(HTTPStatus.FORBIDDEN, {"error": "loopback only"})
            return
        route = urlparse(self.path).path
        if route == "/api/sync":
            self._json(HTTPStatus.OK, self.app.sync_now())
            return
        if route not in ("/api/approve", "/api/reject"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad content length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "json object required"})
            return
        if route == "/api/reject":
            status, result = self.app.reject(payload)
        else:
            status, result = self.app.approve(payload)
        self._json(status, result)


def serve(
    *,
    runtime: Path,
    config_path: Path,
    baseline: float,
    demo: bool,
    cloud: Optional[cloud_api.CloudClient] = None,
    guard: Optional[guard_link.GuardLink] = None,
    sync_status: Optional[dict] = None,
    sync_run: Optional[Any] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    app = App(
        runtime=runtime,
        config_path=config_path,
        baseline=baseline,
        demo=demo,
        cloud=cloud,
        guard=guard,
        sync_status=sync_status,
        sync_run=sync_run,
    )
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
