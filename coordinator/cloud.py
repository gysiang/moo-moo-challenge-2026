from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from dashboard.cloud import CloudClient, CloudError, FILE_ID, SESSION_ID
from coordinator.bundle import BUNDLE_NAME, MAX_BYTES, decode, timestamp
from scout.slots import eastern


def readonly_transport(request, timeout):
    if request.get_method() != "GET":
        raise CloudError(405, "coordinator permits GET only")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise CloudError(413, "cloud response exceeds size limit")
            return int(response.status), body
    except urllib.error.HTTPError as err:
        return err.code, b""


class ReadOnlyCloud(CloudClient):
    def __init__(self, token: str, **kwargs):
        kwargs.setdefault("transport", readonly_transport)
        super().__init__(token, **kwargs)

    def _request(self, method, path, **kwargs):
        if method != "GET":
            raise CloudError(405, "coordinator cannot send messages or change cloud state")
        return super()._request(method, path, **kwargs)

    def _pages(self, path, params):
        cursor = None
        seen = set()
        for _ in range(100):
            page = self._request("GET", path, params={**params, "after_id": cursor})
            rows = page.get("data")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise CloudError(502, "invalid cloud page")
            yield from rows
            if page.get("has_more") is False:
                return
            if page.get("has_more") is not True:
                raise CloudError(502, "cloud pagination status missing")
            cursor = page.get("last_id")
            if not isinstance(cursor, str) or not cursor or cursor in seen or not rows:
                raise CloudError(502, "cloud pagination stalled")
            seen.add(cursor)
        raise CloudError(502, "cloud pagination exceeded limit")

    def iter_sessions(self, agent_id):
        return self._pages("/sessions", {"agent_id": agent_id, "limit": 50, "order": "desc"})

    def iter_files(self, session_id):
        if not SESSION_ID.fullmatch(session_id):
            raise CloudError(400, "invalid session id")
        return self._pages("/files", {"scope_id": session_id, "limit": 100, "order": "asc"})


def poll_once(client, store, *, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    control = store.status()
    summary = {"mode": "DRY_RUN", "trading_enabled": False, "guard_halted": True,
               "baseline_missing": eastern(now).date().isoformat() not in control["baseline_days"],
               "sessions": 0, "bundles": 0, "decisions": [], "errors": []}
    if control["revoked_at"] or not timestamp(control["starts_at"]) <= now < timestamp(control["expires_at"]):
        summary["inactive"] = True
        return summary
    try:
        sessions = list(client.iter_sessions(control["scout_agent_id"]))
        recent = []
        for session in sessions:
            if (session.get("agent") or {}).get("id") != control["scout_agent_id"]:
                continue
            sid = session.get("id", "")
            if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
                raise ValueError("invalid session metadata")
            updated = timestamp(session["updated_at"])
            if now - timedelta(hours=24) <= updated <= now + timedelta(minutes=5):
                recent.append(session)
        recent.sort(key=lambda s: (timestamp(s["updated_at"]), s["id"]))
        for session in recent:
            sid = session["id"]
            summary["sessions"] += 1
            files = list(client.iter_files(sid))
            for meta in files:
                name = meta.get("filename", "")
                if not isinstance(name, str) or not BUNDLE_NAME.fullmatch(name):
                    continue
                fid = meta.get("id", "")
                if not isinstance(fid, str) or not FILE_ID.fullmatch(fid):
                    raise ValueError("invalid file metadata")
                if meta.get("scope_id", sid) != sid:
                    raise ValueError("artifact belongs to a different session")
                if store.artifact_seen(fid):
                    continue
                body = client.download_file(fid)
                bundle = decode(body)
                local = eastern(timestamp(bundle["cycle"]["generated_at"]))
                expected = f"coordinator-{local:%Y%m%d}-{bundle['cycle']['slot'].replace(':', '')}"
                if name not in (expected + ".json", expected + "-rerun.json"):
                    raise ValueError("artifact name does not match its cycle")
                result = store.process(body, session_id=sid, file_id=fid, now=clock())
                summary["bundles"] += 1
                summary["decisions"].extend(result["decisions"])
    except (CloudError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        # API payloads and signed URLs must not be echoed into an audit log.
        summary["errors"].append("cloud intake failed; no dispatch attempted")
    store.record_poll(clock(), {k: v for k, v in summary.items() if k != "decisions"})
    return summary
