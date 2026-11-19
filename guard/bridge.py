from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from gate import Approval, Evidence, ExitMandate, Instrument, Proposal, evaluate
from gate.config import Limits
from gate.models import AccountState

from guard.broker import OrderRequest
from guard.events import EventLog

TOOL_PLACE = "sim_trade_input_order"
DEFAULT_ACC_ID = "5829804"
DEFAULT_MARKET = 100


def parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def order_arguments(
    request: OrderRequest, acc_id: str = DEFAULT_ACC_ID, market: int = DEFAULT_MARKET
) -> dict[str, Any]:
    return {
        "acc_id": acc_id,
        "market": market,
        "symbol": request.symbol,
        "order_type": 1,
        "order_side": 1 if request.side == "BUY" else 2,
        "qty": str(request.quantity),
        "price": f"{request.limit_price:.4f}",
        "text": f"qdr:{request.ticket_id}"[:100],
    }


def proposal_from_record(record: dict) -> Proposal:
    return Proposal(
        ticket_id=record["ticket_id"],
        created_at=parse_time(record["created_at"]),
        symbol=record["symbol"],
        side=record["side"],
        quantity=int(record["quantity"]),
        order_type=record["order_type"],
        limit_price=float(record["limit_price"]),
        est_cost=float(record["est_cost"]),
        starter_tranche=1,
        rationale=record.get("rationale", ""),
        exit_reason=record.get("exit_reason", ""),
        evidence=tuple(
            Evidence(
                evidence_id=item["id"],
                kind=item["kind"],
                as_of=parse_time(item["as_of"]),
                source=item["source"],
            )
            for item in record.get("evidence", [])
        ),
    )


def instrument_from_record(record: dict) -> Instrument:
    row = record["instrument"]
    return Instrument(
        symbol=row["symbol"],
        instrument_type=row["instrument_type"],
        us_listed=bool(row["us_listed"]),
        last_price=float(row["last_price"]),
        market_cap=float(row["market_cap"]),
        tradable=bool(row["tradable"]),
    )


def instrument_from_metrics(
    symbol: str, metrics: dict[str, Any]
) -> Instrument:
    row = metrics["symbols"][symbol]
    return Instrument(
        symbol=symbol,
        instrument_type=row.get("instrument_type", "stock"),
        us_listed=bool(row.get("us_listed", True)),
        last_price=float(row["last_price"]),
        market_cap=float(row["market_cap"]),
        tradable=bool(row.get("tradable", True)),
    )


def instrument_for(record: dict, metrics: Optional[dict[str, Any]]) -> Instrument:
    if "instrument" in record:
        return instrument_from_record(record)
    if metrics is None:
        raise ValueError(f"{record['ticket_id']}: no instrument facts and no metrics file")
    return instrument_from_metrics(record["symbol"], metrics)


def prepare_order(
    record: dict,
    state: AccountState,
    limits: Limits,
    metrics: Optional[dict[str, Any]] = None,
    acc_id: str = DEFAULT_ACC_ID,
    market: int = DEFAULT_MARKET,
    mandate: Optional[ExitMandate] = None,
    run_key: str = "",
    exits_this_run: int = 0,
) -> dict[str, Any]:
    """Re-run the gate at execution time and emit the exact MCP tool call."""
    proposal = proposal_from_record(record)
    instrument = instrument_for(record, metrics)
    gate = evaluate(
        proposal,
        instrument,
        replace(state, mandated_exits_this_run=exits_this_run),
        limits,
        mandate,
    )
    plan: dict[str, Any] = {
        "ticket_id": proposal.ticket_id,
        "proposal_hash": proposal.canonical_hash(),
        "exit_reason": proposal.exit_reason,
        "run_key": run_key,
        "gate": {
            "passed": gate.passed,
            "codes": list(gate.codes),
            "checked": list(gate.checked),
        },
    }
    if not gate.passed:
        plan["status"] = "REJECTED"
        failure = gate.first_failure
        plan["reason"] = failure.code if failure else ""
        return plan
    if mandate is not None and proposal.side == "SELL" and proposal.exit_reason:
        plan["authorization"] = "MANDATE"
        plan["mandate_id"] = mandate.mandate_id
    else:
        plan["authorization"] = "APPROVAL"
    request = OrderRequest(
        ticket_id=proposal.ticket_id,
        symbol=proposal.symbol,
        side=proposal.side,
        quantity=proposal.quantity,
        order_type=proposal.order_type,
        limit_price=round(proposal.limit_price, 6),
    )
    plan["status"] = "READY"
    plan["tool"] = TOOL_PLACE
    plan["arguments"] = order_arguments(request, acc_id, market)
    return plan


def record_submission(plan: dict, result: Any, log: EventLog) -> dict[str, Any]:
    if plan.get("status") != "READY":
        raise ValueError("order plan is not READY")
    arguments = plan["arguments"]
    ticket_id = plan["ticket_id"]
    symbol = arguments["symbol"]
    if not isinstance(result, dict):
        log.emit("ORDER_UNKNOWN", ticket_id=ticket_id, symbol=symbol, error="malformed result")
        log.emit("HALTED", reason="unconfirmed broker outcome")
        return {"status": "UNKNOWN"}

    code = result.get("ret_code", result.get("retCode", 0))
    if code not in (0, "0", None):
        message = result.get("ret_msg") or result.get("retMsg") or ""
        reason = f"broker: {code}: {message}"
        log.emit("ORDER_REJECTED", ticket_id=ticket_id, symbol=symbol, codes=[], reason=reason)
        return {"status": "REJECTED", "reason": reason}

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    order_id = data.get("order_id", data.get("order_id_ex"))
    if order_id in (None, ""):
        log.emit("ORDER_UNKNOWN", ticket_id=ticket_id, symbol=symbol, error="no order_id in result")
        log.emit("HALTED", reason="unconfirmed broker outcome")
        return {"status": "UNKNOWN"}

    log.emit(
        "ORDER_SUBMITTED",
        ticket_id=ticket_id,
        order_id=str(order_id),
        symbol=symbol,
        side="BUY" if arguments["order_side"] == 1 else "SELL",
        quantity=int(arguments["qty"]),
        limit_price=float(arguments["price"]),
        exit_reason=plan.get("exit_reason", ""),
        run_key=plan.get("run_key", ""),
        authorization=plan.get("authorization", "APPROVAL"),
    )
    return {"status": "SUBMITTED", "order_id": str(order_id)}


def record_reconciliation(
    log: EventLog,
    order_id: str,
    submitted: int,
    filled: int,
    status: str,
    position_quantity: int,
) -> dict[str, Any]:
    unfilled = max(submitted - filled, 0)
    log.emit(
        "RECONCILED",
        order_id=order_id,
        submitted=submitted,
        filled=filled,
        unfilled=unfilled,
        status=status,
        position_quantity=position_quantity,
    )
    return {
        "order_id": order_id,
        "submitted": submitted,
        "filled": filled,
        "unfilled": unfilled,
        "status": status,
        "position_quantity": position_quantity,
    }


def record_kill_switch(
    log: EventLog,
    reason: str,
    cancelled: tuple[str, ...] = (),
    failed: tuple[str, ...] = (),
) -> dict[str, Any]:
    log.emit(
        "KILL_SWITCH",
        reason=reason,
        cancelled=list(cancelled),
        failed=list(failed),
    )
    return {"reason": reason, "cancelled": list(cancelled), "failed": list(failed)}


def record_halt(log: EventLog, reason: str) -> None:
    log.emit("HALTED", reason=reason)


def record_resume(log: EventLog, note: str) -> None:
    log.emit("RESUME", note=note)


def approval_for(record: dict, payload: dict[str, Any]) -> Optional[Approval]:
    row = payload.get(record["ticket_id"])
    if not row:
        return None
    return Approval(
        ticket_id=record["ticket_id"],
        proposal_hash=row["proposal_hash"],
        approved_at=parse_time(row["approved_at"]),
        expires_at=parse_time(row["expires_at"]) if row.get("expires_at") else None,
    )
