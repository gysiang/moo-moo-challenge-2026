from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from coordinator.bundle import timestamp
from coordinator.cloud import ReadOnlyCloud, poll_once
from coordinator.store import Store
from dashboard.cloud import TOKEN_KEY, load_env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cloud coordinator rehearsal; trading is permanently disabled in this build")
    parser.add_argument("--db", required=True, help="existing SQLite file on a persistent local volume; never ephemeral QCA storage")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-dry-run", help="create a NEW rehearsal ledger; never a trading mandate")
    init.add_argument("--pilot-id", required=True)
    init.add_argument("--starts-at", required=True, help="fixed ISO timestamp; expires exactly 24 hours later")
    init.add_argument("--scout-agent-id", required=True)
    baseline = commands.add_parser("baseline", help="pin an operator-verified daily starting equity; never inferred from a bundle")
    baseline.add_argument("--day", required=True, help="ET trading day YYYY-MM-DD")
    baseline.add_argument("--equity", required=True, type=float)
    baseline.add_argument("--observed-at", required=True)
    baseline.add_argument("--source", required=True, help="short provenance reference, not credentials or raw broker output")
    commands.add_parser("status")
    commands.add_parser("revoke", help="irreversibly stop this rehearsal; not a broker action")
    run = commands.add_parser("run", help="read delivered bundles and persist hypothetical reservations; cannot dispatch")
    run.add_argument("--env-file", required=True, help="server-side read-only .env containing QODER_PAT_TKN")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=float, default=120)
    run.add_argument("--log-file", type=Path, help="append redacted JSON status to this local file")
    args = parser.parse_args(argv)
    with ExitStack() as stack:
        if args.command == "run" and args.log_file:
            try:
                output = stack.enter_context(args.log_file.open("a", encoding="utf-8", buffering=1))
            except OSError:
                print(json.dumps({"mode": "DRY_RUN", "trading_enabled": False,
                                  "message": "cannot open local worker log; coordinator not started"}))
                return 2
            stack.enter_context(redirect_stdout(output))
        return _execute(args)


def _execute(args) -> int:
    try:
        if args.command == "init-dry-run":
            store = Store.initialize(args.db, pilot_id=args.pilot_id, starts_at=timestamp(args.starts_at),
                                     scout_agent_id=args.scout_agent_id)
        else:
            store = Store(args.db)
            store.status()
        if args.command == "baseline":
            store.add_baseline(args.day, args.equity, timestamp(args.observed_at), args.source)
        elif args.command == "revoke":
            store.revoke(datetime.now(timezone.utc))
        elif args.command == "run":
            if not 10 <= args.interval <= 3600:
                raise ValueError("interval must be between 10 and 3600 seconds")
            token = load_env(Path(args.env_file)).get(TOKEN_KEY, "").strip()
            if not token:
                raise ValueError("server-side cloud token is not configured")
            client = ReadOnlyCloud(token)
            while True:
                result = poll_once(client, store)
                print(json.dumps(result, sort_keys=True), flush=True)
                if args.once or result.get("inactive"):
                    return 1 if result["errors"] else 0
                if result["errors"]:
                    return 1
                time.sleep(args.interval)
        print(json.dumps(store.status(), sort_keys=True))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"mode": "DRY_RUN", "trading_enabled": False,
                          "error": type(exc).__name__, "message": "coordinator stopped; check inputs and persistent storage"}))
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
