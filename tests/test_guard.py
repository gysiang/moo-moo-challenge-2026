from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from gate import Position
from guard import (
    ACCEPTED,
    BLOCKED,
    EXISTING,
    REJECTED,
    UNKNOWN,
    BrokerAck,
    BrokerError,
    EventLog,
    Guard,
    OrderState,
    UnknownOutcome,
    read_events,
    replay,
)
from tests.test_gate import (
    LIMITS,
    approve,
    fresh_quote,
    make_instrument,
    make_proposal,
    make_state,
)


class FakeBroker:
    def __init__(
        self,
        fills: dict[str, int] | None = None,
        cancel_fails: tuple[str, ...] = (),
    ) -> None:
        self.orders: dict[str, OrderState] = {}
        self._positions: dict[str, Position] = {}
        self._seq = 0
        self.fills = fills or {}
        self.cancel_fails = set(cancel_fails)
        self.submission_count = 0
        self.fail_next_submit = False

    def submit(self, request) -> BrokerAck:
        if self.fail_next_submit:
            self.fail_next_submit = False
            raise UnknownOutcome("timeout waiting for acknowledgement")
        self._seq += 1
        order_id = f"O-{self._seq}"
        fill_qty = min(self.fills.get(request.ticket_id, 0), request.quantity)
        if fill_qty >= request.quantity:
            status = "FILLED"
        elif fill_qty > 0:
            status = "PARTIAL"
        else:
            status = "ACKED"
        self.orders[order_id] = OrderState(
            order_id=order_id,
            ticket_id=request.ticket_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            filled_quantity=fill_qty,
            avg_fill_price=request.limit_price if fill_qty else 0.0,
            status=status,
        )
        if fill_qty:
            self._apply_fill(request.symbol, fill_qty, request.limit_price)
        self.submission_count += 1
        return BrokerAck(order_id, "ACKED")

    def cancel(self, order_id: str) -> None:
        if order_id in self.cancel_fails:
            raise BrokerError(f"cancel rejected for {order_id}")
        self.orders[order_id] = replace(self.orders[order_id], status="CANCELLED")

    def order(self, order_id: str) -> OrderState:
        return self.orders[order_id]

    def working_orders(self) -> list[OrderState]:
        return [o for o in self.orders.values() if o.is_working]

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def _apply_fill(self, symbol: str, quantity: int, price: float) -> None:
        held = self._positions.get(symbol)
        prior_qty = held.quantity if held else 0
        prior_cost = (held.average_cost if held else 0.0) * prior_qty
        new_qty = prior_qty + quantity
        avg_cost = (prior_cost + quantity * price) / new_qty
        self._positions[symbol] = Position(
            symbol=symbol,
            quantity=new_qty,
            market_value=new_qty * price,
            average_cost=avg_cost,
        )


class GuardTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "guard.jsonl"

    def make_guard(self, broker: FakeBroker) -> tuple[Guard, EventLog]:
        log = EventLog(self.log_path)
        return Guard(broker, LIMITS, log), log


class TestPartialFillReconciliation(GuardTestBase):
    def test_t6_partial_fill_is_reconciled(self) -> None:
        proposal = make_proposal(quantity=100, limit_price=231.40, est_cost=23_140.0)
        broker = FakeBroker(fills={proposal.ticket_id: 60})
        guard, log = self.make_guard(broker)

        result = guard.submit(proposal, make_instrument(), make_state(proposal))
        self.assertEqual(result.status, ACCEPTED)

        reconciliation = guard.reconcile(result.order_id)

        self.assertEqual(reconciliation.submitted, 100)
        self.assertEqual(reconciliation.filled, 60)
        self.assertEqual(reconciliation.unfilled, 40)
        self.assertEqual(reconciliation.status, "PARTIAL")
        self.assertEqual(reconciliation.position_quantity, 60)
        self.assertEqual(broker.positions()["US.AAPL"].quantity, 60)

        reconciled_events = [r for r in log.records if r["event"] == "RECONCILED"]
        self.assertEqual(len(reconciled_events), 1)
        self.assertEqual(reconciled_events[0]["unfilled"], 40)

    def test_full_fill_leaves_no_unfilled(self) -> None:
        proposal = make_proposal(quantity=14)
        broker = FakeBroker(fills={proposal.ticket_id: 14})
        guard, _ = self.make_guard(broker)

        result = guard.submit(proposal, make_instrument(), make_state(proposal))
        reconciliation = guard.reconcile(result.order_id)

        self.assertEqual(reconciliation.filled, 14)
        self.assertEqual(reconciliation.unfilled, 0)
        self.assertEqual(reconciliation.status, "FILLED")


class TestKillSwitch(GuardTestBase):
    def test_t7_kill_switch_cancels_and_blocks(self) -> None:
        proposal = make_proposal()
        broker = FakeBroker()
        guard, log = self.make_guard(broker)

        submitted = guard.submit(proposal, make_instrument(), make_state(proposal))
        self.assertEqual(submitted.status, ACCEPTED)
        self.assertEqual(len(broker.working_orders()), 1)

        report = guard.engage_kill_switch("operator stop")

        self.assertEqual(report.cancelled, (submitted.order_id,))
        self.assertEqual(broker.order(submitted.order_id).status, "CANCELLED")
        self.assertEqual(broker.working_orders(), [])
        self.assertTrue(guard.kill_engaged)

        next_proposal = make_proposal(ticket_id="P-NEXT")
        blocked = guard.submit(
            next_proposal, make_instrument(), make_state(next_proposal)
        )
        self.assertEqual(blocked.status, BLOCKED)
        self.assertEqual(blocked.reason, "KILL_SWITCH")
        self.assertEqual(broker.submission_count, 1)

        events = [r["event"] for r in log.records]
        self.assertIn("KILL_SWITCH", events)
        self.assertIn("ORDER_BLOCKED", events)

    def test_cancel_failure_is_logged_and_still_blocks(self) -> None:
        proposal = make_proposal()
        broker = FakeBroker()
        guard, log = self.make_guard(broker)
        submitted = guard.submit(proposal, make_instrument(), make_state(proposal))
        broker.cancel_fails.add(submitted.order_id)

        report = guard.engage_kill_switch("operator stop")

        self.assertEqual(report.failed, (submitted.order_id,))
        self.assertEqual(report.cancelled, ())
        kill_event = [r for r in log.records if r["event"] == "KILL_SWITCH"][0]
        self.assertEqual(kill_event["failed"], [submitted.order_id])
        self.assertEqual(guard.submit(proposal, make_instrument(), make_state(proposal)).status, EXISTING)

    def test_resume_requires_kill_switch_clear(self) -> None:
        broker = FakeBroker()
        guard, _ = self.make_guard(broker)
        guard.engage_kill_switch("stop")
        with self.assertRaises(RuntimeError):
            guard.resume("reconciled")


class TestReplay(GuardTestBase):
    def test_t8_replay_is_byte_identical(self) -> None:
        first = make_proposal(ticket_id="P-1", quantity=14)
        second = make_proposal(
            ticket_id="P-2",
            symbol="US.MSFT",
            quantity=10,
            limit_price=420.0,
            est_cost=4_200.0,
        )
        broker = FakeBroker(fills={"P-1": 9})
        guard, log = self.make_guard(broker)

        accepted = guard.submit(first, make_instrument(), make_state(first))
        guard.reconcile(accepted.order_id)
        expired = replace(
            approve(second), approved_at=second.created_at - timedelta(minutes=31)
        )
        rejected = guard.submit(
            second,
            make_instrument(symbol="US.MSFT"),
            make_state(second, approval=expired),
        )
        self.assertEqual(rejected.status, REJECTED)

        run_one = replay(self.log_path).canonical()
        run_two = replay(self.log_path).canonical()
        self.assertEqual(run_one, run_two)

        state = replay(self.log_path)
        self.assertEqual(len(state.submissions), 1)
        self.assertEqual(state.submissions[0].ticket_id, "P-1")
        self.assertEqual(state.submissions[0].quantity, 14)
        self.assertFalse(state.kill_switch_engaged)
        self.assertFalse(state.halted)

        payload = json.loads(run_one)
        self.assertEqual(
            payload["submissions"],
            [
                {
                    "order_id": "O-1",
                    "ticket_id": "P-1",
                    "symbol": "US.AAPL",
                    "side": "BUY",
                    "quantity": 14,
                    "limit_price": 231.4,
                }
            ],
        )
        self.assertEqual(payload["reconciliations"][0]["unfilled"], 5)

    def test_replay_of_identical_scenarios_matches(self) -> None:
        def run(path: Path) -> str:
            proposal = make_proposal()
            broker = FakeBroker(fills={proposal.ticket_id: 5})
            guard = Guard(broker, LIMITS, EventLog(path))
            result = guard.submit(proposal, make_instrument(), make_state(proposal))
            guard.reconcile(result.order_id)
            return replay(path).canonical()

        one = run(Path(self.tmp.name) / "a.jsonl")
        two = run(Path(self.tmp.name) / "b.jsonl")
        self.assertEqual(one, two)

    def test_event_log_is_append_only_jsonl(self) -> None:
        proposal = make_proposal()
        broker = FakeBroker()
        guard, _ = self.make_guard(broker)
        guard.submit(proposal, make_instrument(), make_state(proposal))

        events = read_events(self.log_path)
        self.assertEqual([e["seq"] for e in events], [0])
        self.assertEqual(events[0]["event"], "ORDER_SUBMITTED")


class TestGuardSafety(GuardTestBase):
    def test_gate_failure_never_reaches_broker(self) -> None:
        proposal = make_proposal()
        expired = replace(
            approve(proposal), approved_at=proposal.created_at - timedelta(minutes=31)
        )
        broker = FakeBroker()
        guard, log = self.make_guard(broker)

        result = guard.submit(
            proposal, make_instrument(), make_state(proposal, approval=expired)
        )

        self.assertEqual(result.status, REJECTED)
        self.assertEqual(result.reason, "R11_NO_APPROVAL")
        self.assertEqual(broker.submission_count, 0)
        self.assertEqual([r["event"] for r in log.records], ["ORDER_REJECTED"])

    def test_definite_broker_rejection_is_logged(self) -> None:
        class RejectingBroker(FakeBroker):
            def submit(self, request):
                raise BrokerError("insufficient buying power")

        proposal = make_proposal()
        broker = RejectingBroker()
        guard, log = self.make_guard(broker)

        result = guard.submit(proposal, make_instrument(), make_state(proposal))

        self.assertEqual(result.status, REJECTED)
        self.assertIn("insufficient buying power", result.reason)
        self.assertFalse(guard.halted)
        rejected = [r for r in log.records if r["event"] == "ORDER_REJECTED"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("broker:", rejected[0]["reason"])

    def test_same_ticket_is_idempotent(self) -> None:
        proposal = make_proposal()
        broker = FakeBroker()
        guard, _ = self.make_guard(broker)

        first = guard.submit(proposal, make_instrument(), make_state(proposal))
        second = guard.submit(proposal, make_instrument(), make_state(proposal))

        self.assertEqual(first.status, ACCEPTED)
        self.assertEqual(second.status, EXISTING)
        self.assertEqual(second.order_id, first.order_id)
        self.assertEqual(broker.submission_count, 1)

    def test_unknown_outcome_halts_further_orders(self) -> None:
        proposal = make_proposal()
        broker = FakeBroker()
        broker.fail_next_submit = True
        guard, log = self.make_guard(broker)

        result = guard.submit(proposal, make_instrument(), make_state(proposal))
        self.assertEqual(result.status, UNKNOWN)
        self.assertTrue(guard.halted)

        other = make_proposal(ticket_id="P-2")
        blocked = guard.submit(other, make_instrument(), make_state(other))
        self.assertEqual(blocked.status, BLOCKED)
        self.assertEqual(blocked.reason, "HALTED")
        self.assertEqual(broker.submission_count, 0)

        events = [r["event"] for r in log.records]
        self.assertIn("ORDER_UNKNOWN", events)
        self.assertIn("HALTED", events)

    def test_resume_after_reconciliation(self) -> None:
        proposal = make_proposal()
        broker = FakeBroker()
        broker.fail_next_submit = True
        guard, _ = self.make_guard(broker)

        guard.submit(proposal, make_instrument(), make_state(proposal))
        guard.resume("broker state reconciled")
        self.assertFalse(guard.halted)

        retry = guard.submit(proposal, make_instrument(), make_state(proposal))
        self.assertEqual(retry.status, ACCEPTED)


if __name__ == "__main__":
    unittest.main()
