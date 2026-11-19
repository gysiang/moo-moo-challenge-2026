from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gate import ExitMandate, load_limits, load_mandate
from guard.bridge import (
    DEFAULT_ACC_ID,
    DEFAULT_MARKET,
    approval_for,
    prepare_order,
    record_halt,
    record_kill_switch,
    record_reconciliation,
    record_resume,
    record_submission,
)
from guard.events import EventLog, mandated_exits_in_run
from scout.account import load_account


def _now(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _find_record(cycle: dict, ticket: str) -> dict:
    for record in cycle["records"]:
        if record["ticket_id"] == ticket:
            return record
    raise SystemExit(f"ticket {ticket} not in cycle report")


def _run_key(cycle: dict) -> str:
    return f"{cycle['generated_at'][:10]}:{cycle['slot']}"


def _mandate(path: str) -> Optional[ExitMandate]:
    try:
        return load_mandate(path)
    except ValueError as exc:
        raise SystemExit(str(exc))


def cmd_prepare(args: argparse.Namespace) -> int:
    now = _now(args.now)
    cycle = json.loads(Path(args.cycle).read_text(encoding="utf-8"))
    record = _find_record(cycle, args.ticket)
    state = load_account(Path(args.account), now)
    approvals = json.loads(Path(args.approvals).read_text(encoding="utf-8"))
    state = replace(state, approval=approval_for(record, approvals))
    metrics = (
        json.loads(Path(args.metrics).read_text(encoding="utf-8"))
        if args.metrics
        else None
    )
    run_key = _run_key(cycle)
    plan = prepare_order(
        record,
        state,
        load_limits(Path(args.config)),
        metrics=metrics,
        acc_id=args.acc_id,
        market=args.market,
        mandate=_mandate(args.mandate) if args.mandate else None,
        run_key=run_key,
        exits_this_run=mandated_exits_in_run(args.log, run_key),
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    print(f"{plan['status']} {plan['ticket_id']} gate={','.join(plan['gate']['codes']) or 'ok'}")
    if plan["status"] == "READY":
        print(f"authorization {plan['authorization']}")
        print(f"tool {plan['tool']} args {json.dumps(plan['arguments'], sort_keys=True)}")
    else:
        print(f"reason {plan.get('reason', '')}")
    return 0 if plan["status"] == "READY" else 2


def cmd_record(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.order).read_text(encoding="utf-8"))
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    log = EventLog(args.log, append=True)
    outcome = record_submission(plan, result, log)
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["status"] == "SUBMITTED" else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    log = EventLog(args.log, append=True)
    result = record_reconciliation(
        log,
        order_id=args.order_id,
        submitted=args.submitted,
        filled=args.filled,
        status=args.status,
        position_quantity=args.position_qty,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    log = EventLog(args.log, append=True)
    cancelled = tuple(x for x in (args.cancelled or "").split(",") if x)
    failed = tuple(x for x in (args.failed or "").split(",") if x)
    print(json.dumps(record_kill_switch(log, args.reason, cancelled, failed), sort_keys=True))
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    record_halt(EventLog(args.log, append=True), args.reason)
    print(f"HALTED {args.reason}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    record_resume(EventLog(args.log, append=True), args.note)
    print(f"RESUME {args.note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guard-bridge", description="Cloud-side Guard bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="re-gate a ticket and emit the MCP order call")
    prepare.add_argument("--cycle", required=True)
    prepare.add_argument("--ticket", required=True)
    prepare.add_argument("--account", required=True)
    prepare.add_argument("--approvals", required=True)
    prepare.add_argument("--metrics")
    prepare.add_argument("--config", default="config.json")
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--now")
    prepare.add_argument("--acc-id", default=DEFAULT_ACC_ID)
    prepare.add_argument("--market", type=int, default=DEFAULT_MARKET)
    prepare.add_argument("--mandate", default="runtime/exit-mandate.json")
    prepare.add_argument("--log", default="runtime/guard-events.jsonl")
    prepare.set_defaults(func=cmd_prepare)

    record = sub.add_parser("record", help="record the moomoo place-order result")
    record.add_argument("--order", required=True)
    record.add_argument("--result", required=True)
    record.add_argument("--log", required=True)
    record.set_defaults(func=cmd_record)

    reconcile = sub.add_parser("reconcile", help="record submitted vs filled vs position")
    reconcile.add_argument("--log", required=True)
    reconcile.add_argument("--order-id", required=True)
    reconcile.add_argument("--submitted", type=int, required=True)
    reconcile.add_argument("--filled", type=int, required=True)
    reconcile.add_argument("--status", required=True)
    reconcile.add_argument("--position-qty", type=int, required=True)
    reconcile.set_defaults(func=cmd_reconcile)

    kill = sub.add_parser("kill", help="record a kill-switch event")
    kill.add_argument("--log", required=True)
    kill.add_argument("--reason", required=True)
    kill.add_argument("--cancelled", default="")
    kill.add_argument("--failed", default="")
    kill.set_defaults(func=cmd_kill)

    halt = sub.add_parser("halt", help="record a halt")
    halt.add_argument("--log", required=True)
    halt.add_argument("--reason", required=True)
    halt.set_defaults(func=cmd_halt)

    resume = sub.add_parser("resume", help="record a resume after reconciliation")
    resume.add_argument("--log", required=True)
    resume.add_argument("--note", required=True)
    resume.set_defaults(func=cmd_resume)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
