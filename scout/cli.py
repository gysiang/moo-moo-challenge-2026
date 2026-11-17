from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from gate import Approval, Evidence, load_limits
from scout.account import load_account, load_approvals, parse_time, save_approval
from scout.cycle import Scout
from scout.marketdata import build_snapshot, load_raw, write_snapshot
from scout.slots import SLOTS, due_slot, eastern
from scout.settings import load_strategy
from scout.watchlist import load_watchlist

DEFAULT_CONFIG = Path("config.json")
DEFAULT_WATCHLIST = Path("scout/watchlist.json")


def _load_news(path: Optional[Path]) -> dict[str, tuple[Evidence, ...]]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        symbol: tuple(
            Evidence(
                evidence_id=item["id"],
                kind="news",
                as_of=parse_time(item["as_of"]),
                source=item.get("source", "moomoo.news_search"),
            )
            for item in items
        )
        for symbol, items in payload.items()
    }


def _build_scout(args: argparse.Namespace) -> Scout:
    limits = load_limits(Path(args.config))
    settings = load_strategy(Path(args.config))
    watchlist = load_watchlist(Path(args.watchlist))
    return Scout(limits, settings, watchlist)


def cmd_snapshot(args: argparse.Namespace) -> int:
    snapshot = write_snapshot(load_raw(Path(args.raw)), Path(args.out))
    print(f"{len(snapshot.metrics)} symbols -> {args.out}")
    for symbol, metrics in sorted(snapshot.metrics.items()):
        print(
            f"  {symbol:<9} last {metrics.last_price:>10.2f}  "
            f"sma20 {metrics.sma20:>9.2f}  sma50 {metrics.sma50:>9.2f}  "
            f"roc20 {metrics.roc20:>+7.2%}  trend_up={metrics.trend_up}"
        )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    scout = _build_scout(args)
    snapshot = build_snapshot(load_raw(Path(args.raw)))
    account = load_account(Path(args.account), now)
    approvals = load_approvals(Path(args.approvals) if args.approvals else None)
    report = scout.run(
        slot=args.slot,
        now=now,
        snapshot=snapshot,
        account=account,
        approvals=approvals,
        news=_load_news(Path(args.news) if args.news else None),
    )

    payload = report.to_dict()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    local = eastern(now)
    print(f"slot {report.slot} | now {local:%Y-%m-%d %H:%M %Z} | trading_day={report.trading_day} "
          f"| entries_allowed={report.entries_allowed}")
    for record in report.records:
        proposal = record.proposal
        codes = ",".join(record.gate.codes) or "ok"
        print(
            f"  [{record.status:<17}] {proposal.ticket_id}  {proposal.side:<4} "
            f"{proposal.symbol:<9} qty {proposal.quantity:<5} @ {proposal.limit_price:>9.2f}  "
            f"(${proposal.est_cost:,.2f})  gate={codes}"
        )
    for skip in report.skipped:
        print(f"  [skip               ] {skip.symbol:<9} {skip.reason}")
    print(f"report -> {args.out}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    report = json.loads(Path(args.cycle).read_text(encoding="utf-8"))
    matches = [r for r in report["records"] if r["ticket_id"] == args.ticket]
    if not matches:
        print(f"ticket {args.ticket} not found in {args.cycle}")
        return 1
    record = matches[0]
    if record["status"] != "AWAITING_APPROVAL" and not args.force:
        print(
            f"ticket {args.ticket} has status {record['status']}; "
            "approve only AWAITING_APPROVAL tickets (use --force to override)"
        )
        return 1
    approval = Approval(
        ticket_id=args.ticket,
        proposal_hash=record["proposal_hash"],
        approved_at=now,
        expires_at=now + timedelta(minutes=args.ttl_minutes),
    )
    save_approval(Path(args.approvals), approval)
    print(
        f"approved {approval.ticket_id} until {approval.expires_at:%Y-%m-%d %H:%M}Z "
        f"-> {args.approvals}"
    )
    return 0


def cmd_due(args: argparse.Namespace) -> int:
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    slot = due_slot(now)
    local = eastern(now)
    print(f"{local:%Y-%m-%d %H:%M %Z} due_slot={slot or 'none'}")
    for known in SLOTS:
        print(f"  {known} America/New_York")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scout", description="Moomoo x Qoder Scout cycle")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(DEFAULT_CONFIG))
    common.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))

    snapshot = sub.add_parser("snapshot", parents=[common], help="build metrics from klines")
    snapshot.add_argument("--raw", required=True)
    snapshot.add_argument("--out", required=True)
    snapshot.set_defaults(func=cmd_snapshot)

    plan = sub.add_parser("plan", parents=[common], help="run one Scout cycle")
    plan.add_argument("--slot", required=True, choices=SLOTS)
    plan.add_argument("--now", help="ISO-8601 timestamp; defaults to now")
    plan.add_argument("--raw", required=True, help="klines snapshot input")
    plan.add_argument("--account", required=True, help="account state JSON")
    plan.add_argument("--news", help="optional news evidence JSON")
    plan.add_argument("--approvals", help="optional approvals JSON")
    plan.add_argument("--out", required=True)
    plan.set_defaults(func=cmd_plan)

    approve = sub.add_parser("approve", help="record human approval for a ticket")
    approve.add_argument("--cycle", required=True, help="cycle report JSON")
    approve.add_argument("--ticket", required=True)
    approve.add_argument("--approvals", default="runtime/approvals.json")
    approve.add_argument("--ttl-minutes", type=int, default=30)
    approve.add_argument("--now")
    approve.add_argument("--force", action="store_true")
    approve.set_defaults(func=cmd_approve)

    due = sub.add_parser("due", help="print the slot due right now")
    due.add_argument("--now")
    due.set_defaults(func=cmd_due)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
