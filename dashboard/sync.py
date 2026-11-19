"""Pull artifacts delivered by Scout/Guard cloud sessions into the local runtime dir."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dashboard.cloud import CloudClient, CloudError, TOKEN_KEY, load_env

AGENT_NAMES = {"moomoo-scout", "moomoo-guard"}
CYCLE_NAME = re.compile(r"^cycle-[A-Za-z0-9-]+\.json$")
SNAPSHOT_NAMES = {"account-5829804.json", "metrics.json", "news.json"}
JSONL_NAME = re.compile(r"^guard-events[A-Za-z0-9-]*\.jsonl$")
MAX_BYTES = 2_000_000


def _serialize(name: str, payload: bytes) -> Optional[str]:
    """Return a rejection reason for a payload that must not be written."""
    if not payload:
        return "empty payload"
    if len(payload) > MAX_BYTES:
        return f"payload exceeds {MAX_BYTES} bytes"
    if name.endswith(".json"):
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "payload is not JSON"
        if not isinstance(data, dict):
            return "payload is not a JSON object"
        if CYCLE_NAME.match(name):
            if not isinstance(data.get("slot"), str) or not isinstance(data.get("records"), list):
                return "cycle payload has no slot/records"
            if _cycle_name(data) is None:
                return "cycle payload has no usable generated_at/slot"
    return None


def _cycle_name(data: dict) -> Optional[str]:
    """Map cycle-0945.json to cycle-YYYYMMDD-0945.json so days never collide."""
    slot = str(data.get("slot") or "")
    digits = slot.replace(":", "")
    if len(digits) != 4 or not digits.isdigit():
        return None
    try:
        generated = datetime.fromisoformat(str(data.get("generated_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"cycle-{generated:%Y%m%d}-{digits}.json"


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(payload)
    temp.replace(path)


def _merge_jsonl(path: Path, payload: bytes) -> int:
    incoming = [line for line in payload.decode("utf-8").splitlines() if line.strip()]
    seen = set()
    if path.exists():
        seen = {line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    fresh = []
    for line in incoming:
        if line not in seen:
            seen.add(line)
            fresh.append(line)
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for line in fresh:
                handle.write(line + "\n")
    return len(fresh)


def _log(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def sync_once(
    client: CloudClient,
    runtime: Path,
    *,
    now: Optional[datetime] = None,
    limit: int = 12,
    max_age_hours: float = 24.0,
) -> dict:
    now = now or datetime.now(timezone.utc)
    runtime = Path(runtime)
    cutoff = now - timedelta(hours=max_age_hours)
    summary: dict[str, Any] = {"sessions": 0, "files": 0, "written": [], "skipped": [], "errors": []}

    try:
        sessions = client.sessions(limit=limit)
    except CloudError as err:
        summary["errors"].append({"stage": "sessions", "error": err.message})
        return summary

    recent = []
    for row in sessions:
        agent = (row.get("agent") or {}).get("name")
        if agent not in AGENT_NAMES:
            continue
        try:
            updated = datetime.fromisoformat(str(row.get("updated_at")).replace("Z", "+00:00"))
        except ValueError:
            updated = None
        if updated is not None and updated < cutoff:
            continue
        recent.append(row)
    summary["sessions"] = len(recent)

    for session in recent:
        session_id = session.get("id")
        if not isinstance(session_id, str):
            continue
        try:
            files = client.files(scope_id=session_id, limit=50)
        except CloudError as err:
            summary["errors"].append({"stage": "files", "session": session_id, "error": err.message})
            continue
        for meta in files:
            name = meta.get("filename")
            file_id = meta.get("id")
            if not isinstance(name, str) or not isinstance(file_id, str):
                continue
            if not (CYCLE_NAME.match(name) or name in SNAPSHOT_NAMES or JSONL_NAME.match(name)):
                continue
            summary["files"] += 1
            try:
                payload = client.download_file(file_id)
            except CloudError as err:
                summary["errors"].append({"stage": "download", "file": name, "error": err.message})
                continue
            reason = _serialize(name, payload)
            if reason:
                summary["skipped"].append({"file": name, "reason": reason})
                continue
            if CYCLE_NAME.match(name):
                target = runtime / _cycle_name(json.loads(payload.decode("utf-8")))
                if target.exists():
                    summary["skipped"].append({"file": target.name, "reason": "already present"})
                    continue
            else:
                target = runtime / name
            if JSONL_NAME.match(target.name):
                appended = _merge_jsonl(target, payload)
                if not appended:
                    summary["skipped"].append({"file": name, "reason": "no new rows"})
                    continue
                action = "append"
            else:
                if target.exists() and target.read_bytes() == payload:
                    summary["skipped"].append({"file": name, "reason": "unchanged"})
                    continue
                _write_atomic(target, payload)
                action = "write"
            row = {
                "t": now.isoformat(),
                "file_id": file_id,
                "file": target.name,
                "session": session_id,
                "action": action,
                "bytes": len(payload),
            }
            _log(runtime / "sync.jsonl", row)
            summary["written"].append(row)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.sync", description="Pull delivered artifacts into runtime/")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--interval", type=float, default=0.0, help="seconds between passes; 0 runs once")
    args = parser.parse_args(argv)

    token = load_env(Path(args.env_file)).get(TOKEN_KEY, "").strip()
    if not token:
        print(f"no {TOKEN_KEY} in {args.env_file}; nothing to sync")
        return 2
    client = CloudClient(token)
    runtime = Path(args.runtime)

    while True:
        summary = sync_once(
            client, runtime, limit=max(1, args.limit), max_age_hours=max(0.1, args.max_age_hours)
        )
        print(
            f"synced {len(summary['written'])} of {summary['files']} files "
            f"from {summary['sessions']} sessions"
        )
        for row in summary["written"]:
            print(f"  {row['action']:<6} {row['file']:<28} {row['bytes']:>8} bytes")
        for row in summary["skipped"]:
            print(f"  skip   {row['file']:<28} {row['reason']}")
        for row in summary["errors"]:
            print(f"  error  {row.get('file') or row.get('stage')}: {row['error']}")
        if args.interval <= 0:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
