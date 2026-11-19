from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from gate import Approval, load_mandate
from guard.bridge import proposal_from_record
from guard.events import mandated_exits_in_run
from scout.account import load_account, parse_time, save_approval
from scout.slots import market_session

AWAITING = "AWAITING_APPROVAL"
READY = "READY"
REJECTED_BY_OPERATOR = "REJECTED_BY_OPERATOR"

TERMINAL_EVENTS = {
    "ORDER_REJECTED",
    "ORDER_BLOCKED",
    "ORDER_EXISTING",
    "ORDER_UNKNOWN",
    "RECONCILED",
}


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def load_limits(config_path: Path) -> dict[str, Any]:
    payload = read_json(config_path) or {}
    return {
        "max_positions": int(payload.get("max_positions", 12)),
        "max_orders_per_day": int(payload.get("max_orders_per_day", 20)),
        "max_single_name_pct": float(payload.get("max_single_name_pct", 0.40)),
        "approval_ttl_minutes": int(payload.get("approval_ttl_minutes", 30)),
        "guard_max_snapshot_age_minutes": int(
            payload.get("guard_max_snapshot_age_minutes", 30)
        ),
    }


def load_approvals(path: Path) -> dict[str, dict]:
    payload = read_json(path) or {}
    return {k: v for k, v in payload.items() if isinstance(v, dict)}


def mandate_summary(path: Path, now: datetime) -> Optional[dict]:
    try:
        mandate = load_mandate(path)
    except ValueError as exc:
        return {"active": False, "error": str(exc)}
    if mandate is None:
        return None
    return {
        "mandate_id": mandate.mandate_id,
        "account_id": mandate.account_id,
        "granted_at": iso(mandate.granted_at),
        "expires_at": iso(mandate.expires_at),
        "allowed_reasons": sorted(mandate.allowed_reasons),
        "max_exits_per_run": mandate.max_exits_per_run,
        "active": mandate.granted_at <= now < mandate.expires_at,
    }


def load_rejections(path: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in read_jsonl(path):
        ticket = row.get("ticket_id")
        if isinstance(ticket, str):
            latest[ticket] = row
    return latest


def load_triggers(path: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in read_jsonl(path):
        ticket = row.get("ticket_id")
        if isinstance(ticket, str):
            latest[ticket] = row
    return latest


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def list_cycles(runtime: Path) -> list[dict]:
    cycles = []
    for path in sorted(runtime.glob("cycle-*.json")):
        payload = read_json(path)
        if not payload or "records" not in payload:
            continue
        cycles.append({"file": path.name, "payload": payload})
    return cycles


def approval_state(ticket_id: str, approvals: dict[str, dict], now: datetime) -> dict:
    row = approvals.get(ticket_id)
    if not row:
        return {"approved": False, "state": None, "expires_at": None}
    try:
        expires = parse_time(row["expires_at"]) if row.get("expires_at") else None
    except (KeyError, TypeError, ValueError):
        expires = None
    expired = bool(expires and expires <= now)
    return {
        "approved": not expired,
        "state": "EXPIRED" if expired else "APPROVED",
        "expires_at": iso(expires),
    }


def position_rows(account: dict, metrics: dict, baseline: float) -> list[dict]:
    prices: dict[str, float] = {}
    for symbol, row in (metrics.get("symbols") or {}).items():
        if isinstance(row, dict) and row.get("last_price"):
            prices[symbol] = float(row["last_price"])
    equity = float(account.get("equity") or 0.0)
    rows = []
    for symbol, row in sorted((account.get("positions") or {}).items()):
        quantity = int(row.get("quantity") or 0)
        average_cost = float(row.get("average_cost") or 0.0)
        market_value = float(row.get("market_value") or 0.0)
        if not market_value and quantity:
            market_value = quantity * prices.get(symbol, average_cost)
        cost_basis = quantity * average_cost
        unrealized = market_value - cost_basis
        rows.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "average_cost": round(average_cost, 2),
                "market_value": round(market_value, 2),
                "unrealized": round(unrealized, 2),
                "unrealized_pct": (unrealized / cost_basis) if cost_basis else 0.0,
                "weight": (market_value / equity) if equity else 0.0,
            }
        )
    return rows


def _append_point(path: Path, point: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(point, sort_keys=True) + "\n")


def equity_series(history_path: Path, account: Optional[dict], now: datetime) -> list[dict]:
    points = [
        {"t": row["t"], "equity": float(row["equity"])}
        for row in read_jsonl(history_path)
        if row.get("t") and row.get("equity") is not None
    ]
    if account and account.get("equity") is not None:
        equity = float(account["equity"])
        last = points[-1]["equity"] if points else None
        if last != equity:
            _append_point(
                history_path,
                {
                    "t": iso(now),
                    "equity": equity,
                    "cash": float(account.get("cash") or 0.0),
                },
            )
            points.append({"t": iso(now), "equity": equity})
    return points


def drawdown_stats(points: list[dict], equity: float) -> dict:
    peak = max([p["equity"] for p in points] + [equity]) if (points or equity) else 0.0
    peak = max(peak, equity)
    current = (equity / peak - 1) if peak else 0.0
    worst = 0.0
    running = 0.0
    for point in points:
        running = max(running, point["equity"])
        if running:
            worst = min(worst, point["equity"] / running - 1)
    if peak:
        worst = min(worst, equity / peak - 1)
    return {"peak": round(peak, 2), "current_pct": current, "max_pct": worst}


def open_orders(account: dict, events: list[dict]) -> list[dict]:
    reconciled = {
        row.get("order_id")
        for row in events
        if row.get("event") == "RECONCILED" and row.get("order_id")
    }
    rows = []
    for order_id, submission in _submissions(events).items():
        if order_id in reconciled:
            continue
        rows.append(
            {
                "kind": "SUBMITTED",
                "ticket_id": submission.get("ticket_id"),
                "order_id": order_id,
                "symbol": submission.get("symbol"),
                "side": submission.get("side"),
                "quantity": submission.get("quantity"),
                "limit_price": submission.get("limit_price"),
                "status": _last_event(events, order_id),
            }
        )
    submitted_keys = {(row["symbol"], (row["side"] or "").upper()) for row in rows}
    for row in account.get("working_orders") or []:
        key = (row.get("symbol"), (row.get("side") or "").upper())
        if key in submitted_keys:
            continue
        rows.append(
            {
                "kind": "WORKING",
                "ticket_id": None,
                "order_id": None,
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "quantity": None,
                "limit_price": None,
                "status": "working",
            }
        )
    return rows


def _submissions(events: list[dict]) -> dict[str, dict]:
    return {
        row["order_id"]: row
        for row in events
        if row.get("event") == "ORDER_SUBMITTED" and row.get("order_id")
    }


def _last_event(events: list[dict], order_id: str) -> str:
    label = "SUBMITTED"
    for row in events:
        if row.get("order_id") != order_id:
            continue
        if row.get("event") in TERMINAL_EVENTS:
            label = row["event"].replace("ORDER_", "")
    return label


def decision_rows(
    cycles: list[dict],
    approvals: dict[str, dict],
    now: datetime,
    rejections: Optional[dict[str, dict]] = None,
    triggers: Optional[dict[str, dict]] = None,
) -> list[dict]:
    rejections = rejections or {}
    triggers = triggers or {}
    rows = []
    for cycle in cycles:
        payload = cycle["payload"]
        for record in payload.get("records", []):
            ticket = record.get("ticket_id")
            status = record.get("status")
            state = approval_state(ticket, approvals, now)
            rejection = rejections.get(ticket)
            if rejection is not None and status == AWAITING:
                effective = REJECTED_BY_OPERATOR
            elif state["state"] == "APPROVED" and status == AWAITING:
                effective = "APPROVED"
            elif state["state"] == "EXPIRED" and status == AWAITING:
                effective = "APPROVAL_EXPIRED"
            else:
                effective = status
            trigger = triggers.get(ticket)
            rows.append(
                {
                    "cycle": cycle["file"],
                    "slot": payload.get("slot"),
                    "generated_at": payload.get("generated_at"),
                    "ticket_id": ticket,
                    "side": record.get("side"),
                    "symbol": record.get("symbol"),
                    "sleeve": record.get("sleeve"),
                    "quantity": record.get("quantity"),
                    "limit_price": record.get("limit_price"),
                    "est_cost": record.get("est_cost"),
                    "status": effective,
                    "raw_status": status,
                    "gate_codes": (record.get("gate") or {}).get("codes", []),
                    "rationale": record.get("rationale"),
                    "exit_reason": record.get("exit_reason", ""),
                    "evidence": record.get("evidence", []),
                    "approval_expires_at": state["expires_at"],
                    "rejected_at": rejection.get("rejected_at") if rejection else None,
                    "rejection_reason": rejection.get("reason") if rejection else None,
                    "guard_state": trigger.get("state") if trigger else None,
                    "guard_triggered_at": (
                        trigger.get("requested_at")
                        if trigger and trigger.get("state") == "accepted"
                        else None
                    ),
                    "guard_session_id": trigger.get("session_id") if trigger else None,
                }
            )
    rows.sort(key=lambda r: (r.get("generated_at") or "", r.get("ticket_id") or ""), reverse=True)
    return rows


def build_state(
    *,
    runtime: Path,
    config_path: Path,
    baseline: float,
    now: Optional[datetime] = None,
    demo: bool = False,
    approvals_file: str = "approvals.json",
    events_file: str = "guard-events.jsonl",
    history_file: str = "equity-history.jsonl",
    rejections_file: str = "rejections.jsonl",
    triggers_file: str = "guard-triggers.jsonl",
) -> dict:
    now = now or datetime.now(timezone.utc)
    account = read_json(runtime / "account-5829804.json") or {}
    metrics = read_json(runtime / "metrics.json") or {}
    approvals = load_approvals(runtime / approvals_file)
    rejections = load_rejections(runtime / rejections_file)
    triggers = load_triggers(runtime / triggers_file)
    cycles = list_cycles(runtime)
    events = read_jsonl(runtime / events_file)
    limits = load_limits(config_path)

    equity = float(account.get("equity") or 0.0)
    cash = float(account.get("cash") or 0.0)
    day_open = float(account.get("day_open_equity") or equity or 0.0)
    positions = position_rows(account, metrics, baseline)
    series = equity_series(runtime / history_file, account or None, now)
    dd = drawdown_stats(series, equity)
    decisions = decision_rows(cycles, approvals, now, rejections, triggers)

    return {
        "demo": demo,
        "as_of": iso(now),
        "market": market_session(now),
        "has_snapshot": bool(account),
        "account_id": account.get("account_id"),
        "account_as_of": metrics.get("as_of"),
        "kpi": {
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "market_value": round(sum(p["market_value"] for p in positions), 2),
            "day_pnl_pct": (equity / day_open - 1) if day_open else 0.0,
            "total_pnl": round(equity - baseline, 2),
            "total_pnl_pct": ((equity / baseline - 1) if baseline else 0.0),
            "baseline": baseline,
            "drawdown_pct": dd["current_pct"],
            "max_drawdown_pct": dd["max_pct"],
            "peak_equity": dd["peak"],
            "positions_used": len(positions),
            "positions_limit": limits["max_positions"],
            "orders_used": int(account.get("executed_orders_today") or 0),
            "orders_limit": limits["max_orders_per_day"],
        },
        "equity_series": series,
        "positions": positions,
        "open_orders": open_orders(account, events),
        "decisions": decisions,
        "approvals": approvals,
        "counts": {
            "awaiting": sum(1 for d in decisions if d["status"] == AWAITING),
            "approved": sum(1 for d in decisions if d["status"] == "APPROVED"),
            "rejected": sum(1 for d in decisions if d["status"] == "REJECTED"),
            "operator_rejected": sum(
                1 for d in decisions if d["status"] == REJECTED_BY_OPERATOR
            ),
            "ready": sum(1 for d in decisions if d["status"] == READY),
        },
        "guard": {
            "halted": any(e.get("event") == "HALTED" for e in events),
            "kill_switch": any(e.get("event") == "KILL_SWITCH" for e in events),
            "events": len(events),
        },
        "limits": limits,
        "exit_mandate": mandate_summary(runtime / "exit-mandate.json", now),
    }


def approve_ticket(
    *,
    runtime: Path,
    cycle_file: str,
    ticket_id: str,
    ttl_minutes: int,
    now: Optional[datetime] = None,
    approvals_file: str = "approvals.json",
    rejections_file: str = "rejections.jsonl",
) -> dict:
    now = now or datetime.now(timezone.utc)
    path = Path(cycle_file).name
    cycles = {c["file"]: c["payload"] for c in list_cycles(runtime)}
    payload = cycles.get(path)
    if payload is None:
        return {"ok": False, "error": f"cycle {path} not found in {runtime}"}
    record = next(
        (r for r in payload.get("records", []) if r.get("ticket_id") == ticket_id), None
    )
    if record is None:
        return {"ok": False, "error": f"ticket {ticket_id} not in {path}"}
    if record.get("status") != AWAITING:
        return {
            "ok": False,
            "error": f"ticket {ticket_id} has status {record.get('status')}; only AWAITING_APPROVAL can be approved",
        }
    if not record.get("proposal_hash"):
        return {"ok": False, "error": "cycle record has no proposal_hash; re-run the cycle"}
    if ticket_id in load_rejections(runtime / rejections_file):
        return {"ok": False, "error": f"ticket {ticket_id} was rejected in the dashboard"}

    approval = Approval(
        ticket_id=ticket_id,
        proposal_hash=record["proposal_hash"],
        approved_at=now,
        expires_at=now + timedelta(minutes=max(1, int(ttl_minutes))),
    )
    save_approval(runtime / approvals_file, approval)
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "approved_at": iso(approval.approved_at),
        "expires_at": iso(approval.expires_at),
    }


def authorize_exit(
    *,
    runtime: Path,
    cycle_file: str,
    ticket_id: str,
    now: Optional[datetime] = None,
    events_file: str = "guard-events.jsonl",
) -> Optional[dict]:
    """Authorization for a mandated risk-reducing exit; None when no mandate applies."""
    now = now or datetime.now(timezone.utc)
    payload = read_json(runtime / Path(cycle_file).name) or {}
    record = next(
        (r for r in payload.get("records", []) if r.get("ticket_id") == ticket_id), None
    )
    if record is None or record.get("side") != "SELL" or not record.get("exit_reason"):
        return None
    if record.get("status") == "REJECTED":
        return {
            "ok": False,
            "error": f"ticket {ticket_id} was rejected by the gate; re-run the cycle",
        }
    try:
        mandate = load_mandate(runtime / "exit-mandate.json")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if mandate is None:
        return None
    account_path = runtime / "account-5829804.json"
    if not account_path.exists():
        return {"ok": False, "error": "account snapshot account-5829804.json is missing"}
    run_key = f"{str(payload.get('generated_at', ''))[:10]}:{payload.get('slot', '')}"
    account = replace(
        load_account(account_path, now),
        mandated_exits_this_run=mandated_exits_in_run(runtime / events_file, run_key),
    )
    rejection = mandate.covers(proposal_from_record(record), account)
    if rejection is not None:
        return {
            "ok": False,
            "error": f"exit mandate does not cover {ticket_id}: {rejection.message}",
        }
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "authorization": "MANDATE",
        "mandate_id": mandate.mandate_id,
        "exit_reason": record["exit_reason"],
    }


def reject_ticket(
    *,
    runtime: Path,
    cycle_file: str,
    ticket_id: str,
    reason: str = "",
    now: Optional[datetime] = None,
    approvals_file: str = "approvals.json",
    rejections_file: str = "rejections.jsonl",
) -> dict:
    now = now or datetime.now(timezone.utc)
    path = Path(cycle_file).name
    cycles = {c["file"]: c["payload"] for c in list_cycles(runtime)}
    payload = cycles.get(path)
    if payload is None:
        return {"ok": False, "error": f"cycle {path} not found in {runtime}"}
    record = next(
        (r for r in payload.get("records", []) if r.get("ticket_id") == ticket_id), None
    )
    if record is None:
        return {"ok": False, "error": f"ticket {ticket_id} not in {path}"}
    if record.get("status") != AWAITING:
        return {
            "ok": False,
            "error": f"ticket {ticket_id} has status {record.get('status')}; only AWAITING_APPROVAL can be rejected",
        }
    if ticket_id in load_rejections(runtime / rejections_file):
        return {"ok": True, "ticket_id": ticket_id, "already_rejected": True}
    approval = approval_state(ticket_id, load_approvals(runtime / approvals_file), now)
    if approval["approved"]:
        return {
            "ok": False,
            "error": f"ticket {ticket_id} has a live approval until {approval['expires_at']}; it can no longer be rejected",
        }

    row = {
        "ticket_id": ticket_id,
        "cycle_file": path,
        "proposal_hash": record.get("proposal_hash"),
        "symbol": record.get("symbol"),
        "side": record.get("side"),
        "quantity": record.get("quantity"),
        "limit_price": record.get("limit_price"),
        "est_cost": record.get("est_cost"),
        "gate_codes": (record.get("gate") or {}).get("codes", []),
        "rejected_at": iso(now),
        "actor": "dashboard-operator",
        "reason": (reason or "").strip()[:200] or "operator rejected in dashboard",
    }
    append_jsonl(runtime / rejections_file, row)
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "rejected_at": row["rejected_at"],
        "reason": row["reason"],
    }
