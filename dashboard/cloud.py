from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

BASE_URL = "https://api.qoder.com/api/v1/cloud"
TOKEN_KEY = "QODER_PAT_TKN"
SESSION_ID = re.compile(r"^sess_[A-Za-z0-9]+$")
FILE_ID = re.compile(r"^file_[A-Za-z0-9]+$")
NOISE_EVENTS = {"span.model_request_start", "span.model_request_end", "session.updated"}

Transport = Callable[[urllib.request.Request, float], "tuple[int, bytes]"]


class CloudError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def load_env(path: str | Path) -> dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in values:
            values[key] = value
    return values


def _urlopen(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read() or b""


class CloudClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or _urlopen

    def _redact(self, message: str) -> str:
        return message.replace(self._token, "[redacted]") if self._token else message

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        payload: Optional[dict] = None,
    ) -> dict:
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                query = "?" + urllib.parse.urlencode(clean, doseq=True)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "moomoo-dashboard/1",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._base_url + path + query, data=data, headers=headers, method=method
        )
        try:
            status, body = self._transport(request, self._timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise CloudError(0, self._redact(f"cloud API unreachable: {reason}")) from None

        payload: Any = None
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None

        if not 200 <= status < 300:
            message = ""
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "")
                elif isinstance(error, str):
                    message = error
                message = message or str(payload.get("message") or "")
            raise CloudError(status, self._redact(message or f"cloud API returned HTTP {status}"))
        if not isinstance(payload, dict):
            raise CloudError(status, "cloud API returned a non-JSON body")
        return payload

    def sessions(self, *, limit: int = 12, agent_id: Optional[str] = None) -> list[dict]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 50)), "order": "desc"}
        if agent_id:
            params["agent_id"] = agent_id
        data = self._request("GET", "/sessions", params=params).get("data")
        return [row for row in data or [] if isinstance(row, dict)]

    def session_events(
        self, session_id: str, *, limit: int = 40, types: Optional[list[str]] = None
    ) -> list[dict]:
        if not SESSION_ID.match(session_id or ""):
            raise CloudError(400, "invalid session id")
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100)), "order": "desc"}
        if types:
            params["types"] = ",".join(types)
        data = self._request("GET", f"/sessions/{session_id}/events", params=params).get("data")
        return [row for row in data or [] if isinstance(row, dict)]

    def files(
        self,
        *,
        limit: int = 20,
        scope_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100)), "order": "desc"}
        if scope_id:
            if not SESSION_ID.match(scope_id):
                raise CloudError(400, "invalid scope_id")
            params["scope_id"] = scope_id
        if name:
            params["name"] = name
        data = self._request("GET", "/files", params=params).get("data")
        return [row for row in data or [] if isinstance(row, dict)]

    def file(self, file_id: str) -> dict:
        if not FILE_ID.match(file_id or ""):
            raise CloudError(400, "invalid file id")
        payload = self._request("GET", f"/files/{file_id}")
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        return item if isinstance(item, dict) else {}

    def file_content(self, file_id: str) -> dict:
        if not FILE_ID.match(file_id or ""):
            raise CloudError(400, "invalid file id")
        payload = self._request("GET", f"/files/{file_id}/content")
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CloudError(502, "file content response has no https download url")
        return {"url": url, "expires_at": payload.get("expires_at")}

    def download_file(self, file_id: str) -> bytes:
        info = self.file_content(file_id)
        request = urllib.request.Request(
            info["url"], headers={"User-Agent": "moomoo-dashboard/1"}, method="GET"
        )
        try:
            status, body = self._transport(request, self._timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise CloudError(0, self._redact(f"artifact download unreachable: {reason}")) from None
        if not 200 <= status < 300:
            raise CloudError(status, f"artifact download returned HTTP {status}")
        return body

    def _forward(self) -> "CloudClient":
        return CloudClient(self._token, base_url="https://api.qoder.com/api/v1/forward",
                           timeout=self._timeout, transport=self._transport)

    def create_forward_session(self, identity_id: str, template_id: str, title: str) -> dict:
        payload = self._forward()._request("POST", "/sessions", payload={
            "identity_id": identity_id, "template_id": template_id, "title": title,
        })
        item = payload.get("item", payload)
        if not isinstance(item, dict) or not SESSION_ID.fullmatch(str(item.get("id", ""))):
            raise CloudError(0, "Session creation returned no valid id; inspect Cloud Agents before retrying")
        return item

    def guard_batch_ready(self, template_id: str) -> bool:
        if not re.fullmatch(r"tmpl_[A-Za-z0-9]+", template_id):
            raise CloudError(400, "invalid template id")
        payload = self._forward()._request("GET", f"/templates/{template_id}")
        item = payload.get("item", payload)
        if not isinstance(item, dict):
            return False
        system = item.get("system", "")
        tools = item.get("tools", [])
        return (item.get("status") == "active" and isinstance(system, str) and "guard-batch" in system and
                "One approved batch runs serially" in system and isinstance(tools, list) and
                any(isinstance(t, dict) and isinstance(t.get("enabled_tools"), list) and
                    "DeliverArtifacts" in t["enabled_tools"] for t in tools))

    def forward_session(self, session_id: str) -> dict:
        if not SESSION_ID.fullmatch(session_id):
            raise CloudError(400, "invalid session id")
        payload = self._forward()._request("GET", f"/sessions/{session_id}")
        return payload.get("item", payload)

    def send_forward_message(self, session_id: str, text: str) -> dict:
        return self._forward().send_user_message(session_id, text)

    def send_user_message(self, session_id: str, text: str, *, limit: int = 256_000) -> dict:
        if not SESSION_ID.match(session_id or ""):
            raise CloudError(400, "invalid session id")
        if not isinstance(text, str) or not text.strip():
            raise CloudError(400, "message text is required")
        if len(text) > limit:
            raise CloudError(413, f"message exceeds {limit} characters")
        return self._request(
            "POST",
            f"/sessions/{session_id}/events",
            payload={
                "events": [
                    {"type": "user.message", "content": [{"type": "text", "text": text}]}
                ]
            },
        )


def _clip(text: str, limit: int = 240) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_session(row: dict) -> dict:
    agent = row.get("agent") if isinstance(row.get("agent"), dict) else {}
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "agent": agent.get("name"),
        "agent_id": agent.get("id"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "deployment_id": row.get("deployment_id"),
        "credits": usage.get("total_credits"),
    }


def summarize_event(row: dict) -> Optional[dict]:
    kind = str(row.get("type") or "event")
    if kind in NOISE_EVENTS:
        return None
    text = ""
    content = row.get("content")
    if isinstance(content, list):
        text = " ".join(
            str(block["text"])
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    if kind.endswith("tool_use"):
        name = str(row.get("name") or "tool")
        args = row.get("input")
        text = f"{name} {json.dumps(args, sort_keys=True) if args is not None else ''}".strip()
    elif kind.endswith("tool_result"):
        result = row.get("result")
        text = json.dumps(result, sort_keys=True) if result is not None else (text or "result")
    elif kind == "session.error":
        error = row.get("error") if isinstance(row.get("error"), dict) else {}
        text = str(error.get("message") or "session error")
    elif kind.startswith("span."):
        return None
    return {
        "id": row.get("id"),
        "type": kind,
        "at": row.get("processed_at"),
        "text": _clip(text),
    }


def summarize_events(rows: list[dict]) -> list[dict]:
    events = (summarize_event(row) for row in rows)
    return [event for event in events if event is not None]
