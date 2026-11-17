from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gate import GateResult, Instrument, Proposal, evaluate
from gate.config import Limits
from gate.models import AccountState

from guard.broker import (
    Broker,
    BrokerAck,
    BrokerError,
    OrderRequest,
    OrderState,
    UnknownOutcome,
)
from guard.events import EventLog

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
BLOCKED = "BLOCKED"
EXISTING = "EXISTING"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GuardResult:
    status: str
    order_id: Optional[str]
    gate: Optional[GateResult]
    reason: Optional[str] = None


@dataclass(frozen=True)
class Reconciliation:
    order_id: str
    submitted: int
    filled: int
    unfilled: int
    status: str
    position_quantity: int


@dataclass(frozen=True)
class KillSwitchReport:
    cancelled: tuple[str, ...]
    failed: tuple[str, ...]
    reason: str


class Guard:
    def __init__(self, broker: Broker, limits: Limits, log: EventLog) -> None:
        self._broker = broker
        self._limits = limits
        self._log = log
        self._order_by_ticket: dict[str, str] = {}
        self._kill_engaged = False
        self._halted = False

    @property
    def kill_engaged(self) -> bool:
        return self._kill_engaged

    @property
    def halted(self) -> bool:
        return self._halted

    def submit(
        self, proposal: Proposal, instrument: Optional[Instrument], state: AccountState
    ) -> GuardResult:
        prior = self._order_by_ticket.get(proposal.ticket_id)
        if prior is not None:
            self._log.emit(
                "ORDER_EXISTING",
                ticket_id=proposal.ticket_id,
                order_id=prior,
            )
            return GuardResult(EXISTING, prior, None, "ticket already submitted")

        if self._kill_engaged:
            return self._blocked(proposal, "KILL_SWITCH")
        if self._halted:
            return self._blocked(proposal, "HALTED")

        gate = evaluate(proposal, instrument, state, self._limits)
        if not gate.passed:
            failure = gate.first_failure
            self._log.emit(
                "ORDER_REJECTED",
                ticket_id=proposal.ticket_id,
                symbol=proposal.symbol,
                codes=list(gate.codes),
                reason=failure.message if failure else "",
            )
            return GuardResult(REJECTED, None, gate, failure.code if failure else None)

        request = OrderRequest(
            ticket_id=proposal.ticket_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            order_type=proposal.order_type,
            limit_price=round(proposal.limit_price, 6),
        )
        try:
            ack = self._broker.submit(request)
        except UnknownOutcome as exc:
            return self._halt(proposal, str(exc))
        except BrokerError as exc:
            self._log.emit(
                "ORDER_REJECTED",
                ticket_id=proposal.ticket_id,
                symbol=proposal.symbol,
                codes=[],
                reason=f"broker: {exc}",
            )
            return GuardResult(REJECTED, None, gate, f"BROKER: {exc}")

        if ack.status != "ACKED":
            return self._halt(proposal, f"broker status {ack.status}")

        self._order_by_ticket[proposal.ticket_id] = ack.order_id
        self._log.emit(
            "ORDER_SUBMITTED",
            ticket_id=proposal.ticket_id,
            order_id=ack.order_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            limit_price=round(proposal.limit_price, 6),
        )
        return GuardResult(ACCEPTED, ack.order_id, gate)

    def reconcile(self, order_id: str) -> Reconciliation:
        order = self._broker.order(order_id)
        positions = self._broker.positions()
        held = positions.get(order.symbol)
        result = Reconciliation(
            order_id=order.order_id,
            submitted=order.quantity,
            filled=order.filled_quantity,
            unfilled=order.unfilled_quantity,
            status=order.status,
            position_quantity=held.quantity if held else 0,
        )
        self._log.emit(
            "RECONCILED",
            order_id=result.order_id,
            submitted=result.submitted,
            filled=result.filled,
            unfilled=result.unfilled,
            status=result.status,
            position_quantity=result.position_quantity,
        )
        return result

    def engage_kill_switch(self, reason: str) -> KillSwitchReport:
        self._kill_engaged = True
        cancelled: list[str] = []
        failed: list[str] = []
        for order in self._broker.working_orders():
            try:
                self._broker.cancel(order.order_id)
                cancelled.append(order.order_id)
            except Exception:
                failed.append(order.order_id)
        report = KillSwitchReport(tuple(cancelled), tuple(failed), reason)
        self._log.emit(
            "KILL_SWITCH",
            reason=reason,
            cancelled=list(report.cancelled),
            failed=list(report.failed),
        )
        return report

    def resume(self, note: str) -> None:
        if self._kill_engaged:
            raise RuntimeError("kill switch must be cleared before resuming")
        self._halted = False
        self._log.emit("RESUME", note=note)

    def _blocked(self, proposal: Proposal, reason: str) -> GuardResult:
        self._log.emit(
            "ORDER_BLOCKED",
            ticket_id=proposal.ticket_id,
            symbol=proposal.symbol,
            reason=reason,
        )
        return GuardResult(BLOCKED, None, None, reason)

    def _halt(self, proposal: Proposal, error: str) -> GuardResult:
        self._halted = True
        self._log.emit(
            "ORDER_UNKNOWN",
            ticket_id=proposal.ticket_id,
            symbol=proposal.symbol,
            error=error,
        )
        self._log.emit("HALTED", reason="unconfirmed broker outcome")
        return GuardResult(UNKNOWN, None, None, error)
