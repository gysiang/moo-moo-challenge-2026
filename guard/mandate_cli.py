from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from gate import EXIT_REASONS, ExitMandate, load_mandate, save_mandate

DEFAULT_PATH = "runtime/exit-mandate.json"
MAX_HOURS = 168


def _now(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _reasons(value: str) -> frozenset[str]:
    if value.strip().lower() == "all":
        return frozenset(EXIT_REASONS)
    picked = frozenset(item.strip() for item in value.split(",") if item.strip())
    unknown = picked - EXIT_REASONS
    if unknown:
        raise SystemExit(f"unknown exit reasons: {sorted(unknown)}; known: {sorted(EXIT_REASONS)}")
    return picked


def _load(path: str) -> Optional[ExitMandate]:
    try:
        return load_mandate(path)
    except ValueError as exc:
        raise SystemExit(str(exc))


def cmd_grant(args: argparse.Namespace) -> int:
    now = _now(args.now)
    path = Path(args.path)
    existing = _load(args.path)
    if existing is not None and existing.expires_at > now and not args.replace:
        raise SystemExit(
            f"an active mandate {existing.mandate_id} expires {existing.expires_at.isoformat()}; "
            "revoke it or pass --replace"
        )
    if args.hours <= 0 or args.hours > MAX_HOURS:
        raise SystemExit(f"--hours must be in (0, {MAX_HOURS}]")
    if not args.note.strip():
        raise SystemExit("--note must state why these exits are pre-authorized")
    if not args.granted_by.strip():
        raise SystemExit("--granted-by must name the operator authorizing the mandate")
    mandate = ExitMandate(
        mandate_id=args.mandate_id or f"M-{args.account_id}-{now:%Y%m%d%H%M%S}",
        account_id=args.account_id,
        granted_at=now,
        expires_at=now + timedelta(hours=args.hours),
        allowed_reasons=_reasons(args.reasons),
        max_exits_per_run=args.max_exits,
        note=f"{args.note.strip()} (granted by {args.granted_by.strip()})",
    )
    save_mandate(path, mandate)
    print(json.dumps(mandate.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    now = _now(args.now)
    mandate = _load(args.path)
    if mandate is None:
        print(f"NO_MANDATE {args.path}")
        return 1
    payload = mandate.to_dict()
    payload["active"] = mandate.granted_at <= now < mandate.expires_at
    payload["seconds_remaining"] = max(int((mandate.expires_at - now).total_seconds()), 0)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["active"] else 1


def cmd_revoke(args: argparse.Namespace) -> int:
    now = _now(args.now)
    path = Path(args.path)
    mandate = _load(args.path)
    if mandate is None:
        print(f"NO_MANDATE {path}")
        return 1
    if not args.reason.strip():
        raise SystemExit("--reason is required to revoke a mandate")
    archive = path.with_name(f"{path.stem}.revoked-{now:%Y%m%d%H%M%S}{path.suffix}")
    path.replace(archive)
    record = {
        "mandate_id": mandate.mandate_id,
        "mandate_hash": mandate.canonical_hash(),
        "revoked_at": now.isoformat(),
        "reason": args.reason.strip(),
        "archived_to": str(archive),
    }
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "MANDATE_REVOKED", **record}, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guard-mandate",
        description="Operator-signed mandate for risk-reducing exits (paper account only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    grant = sub.add_parser("grant", help="sign a bounded exit mandate")
    grant.add_argument("--account-id", required=True)
    grant.add_argument("--hours", type=float, required=True)
    grant.add_argument("--granted-by", required=True)
    grant.add_argument("--note", required=True)
    grant.add_argument("--reasons", default="all")
    grant.add_argument("--max-exits", type=int, default=6)
    grant.add_argument("--mandate-id")
    grant.add_argument("--path", default=DEFAULT_PATH)
    grant.add_argument("--now")
    grant.add_argument("--replace", action="store_true")
    grant.set_defaults(func=cmd_grant)

    show = sub.add_parser("show", help="print the mandate and whether it is active")
    show.add_argument("--path", default=DEFAULT_PATH)
    show.add_argument("--now")
    show.set_defaults(func=cmd_show)

    revoke = sub.add_parser("revoke", help="withdraw the mandate and archive the signed file")
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--path", default=DEFAULT_PATH)
    revoke.add_argument("--log", default="runtime/guard-events.jsonl")
    revoke.add_argument("--now")
    revoke.set_defaults(func=cmd_revoke)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())