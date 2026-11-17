from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gate import Approval, Position, WorkingOrder
from gate.models import AccountState


def parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def load_account(path: Path, now: datetime) -> AccountState:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    equity = float(payload["equity"])
    day_open = float(payload.get("day_open_equity", equity))
    daily_pnl_pct = equity / day_open - 1 if day_open else 0.0
    positions = {
        symbol: Position(
            symbol=symbol,
            quantity=int(row["quantity"]),
            market_value=float(row.get("market_value", 0.0)),
            average_cost=float(row.get("average_cost", 0.0)),
        )
        for symbol, row in payload.get("positions", {}).items()
    }
    working = tuple(
        WorkingOrder(symbol=row["symbol"], side=row["side"])
        for row in payload.get("working_orders", [])
    )
    return AccountState(
        now=now,
        cash=float(payload["cash"]),
        equity=equity,
        positions=positions,
        working_orders=working,
        executed_orders_today=int(payload.get("executed_orders_today", 0)),
        daily_pnl_pct=daily_pnl_pct,
    )


def load_approvals(path: Optional[Path]) -> dict[str, Approval]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    approvals: dict[str, Approval] = {}
    for ticket_id, row in payload.items():
        approvals[ticket_id] = Approval(
            ticket_id=ticket_id,
            proposal_hash=row["proposal_hash"],
            approved_at=parse_time(row["approved_at"]),
            expires_at=parse_time(row["expires_at"]) if row.get("expires_at") else None,
        )
    return approvals


def save_approval(path: Path, approval: Approval) -> None:
    existing: dict = {}
    if Path(path).exists():
        existing = json.loads(Path(path).read_text(encoding="utf-8"))
    existing[approval.ticket_id] = {
        "proposal_hash": approval.proposal_hash,
        "approved_at": approval.approved_at.isoformat(),
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
