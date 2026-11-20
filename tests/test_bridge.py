from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from guard.bridge import (
    approval_for,
    instrument_from_metrics,
    prepare_order,
    record_halt,
    record_kill_switch,
    record_reconciliation,
    record_resume,
    record_submission,
)
from guard.events import EventLog, read_events, replay
from tests.test_gate import LIMITS, NOW, approve, make_instrument, make_proposal, make_state


def record_for(proposal, instrument=None) -> dict:
    record = {
        "sleeve": "satellite",
        "status": "AWAITING_APPROVAL",
        "ticket_id": proposal.ticket_id,
        "proposal_hash": proposal.canonical_hash(),
        "symbol": proposal.symbol,
        "side": proposal.side,
        "quantity": proposal.quantity,
        "order_type": proposal.order_type,
        "limit_price": proposal.limit_price,
        "est_cost": proposal.est_cost,
        "created_at": proposal.created_at.isoformat(),
        "rationale": proposal.rationale,
        "exit_reason": proposal.exit_reason,
        "evidence": [
            {
                "id": item.evidence_id,
                "kind": item.kind,
                "as_of": item.as_of.isoformat(),
                "source": item.source,
            }
            for item in proposal.evidence
        ],
        "gate": {"passed": False, "codes": ["R11_NO_APPROVAL"], "checked": ["R1"]},
    }
    if instrument is not None:
        record["instrument"] = {
            "symbol": instrument.symbol,
            "instrument_type": instrument.instrument_type,
            "us_listed": instrument.us_listed,
            "last_price": instrument.last_price,
            "market_cap": instrument.market_cap,
            "tradable": instrument.tradable,
        }
    return record


def metrics_for(symbol: str, instrument) -> dict:
    return {
        "as_of": NOW.isoformat(),
        "symbols": {
            symbol: {
                "instrument_type": instrument.instrument_type,
                "last_price": instrument.last_price,
                "market_cap": instrument.market_cap,
            }
        },
    }


class TestHaltedPrepareCLI(unittest.TestCase):
    def test_halt_replaces_old_plan_before_reading_other_inputs(self):
        from guard.bridge_cli import cmd_prepare
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal, output = root / "events.jsonl", root / "plan.json"
            journal.write_text('{"event":"HALTED"}\n', encoding="utf-8")
            output.write_text('{"status":"READY","arguments":{"qty":"1"}}', encoding="utf-8")
            args = argparse.Namespace(log=str(journal), out=str(output), ticket="fixture-ticket")
            self.assertEqual(cmd_prepare(args), 2)
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["reason"], "GUARD_HALTED")
            self.assertNotIn("arguments", plan)


class TestPrepareOrder(unittest.TestCase):
    def setUp(self) -> None:
        self.proposal = make_proposal()
        self.instrument = make_instrument()
        self.record = record_for(self.proposal)

    def test_ready_emits_exact_mcp_call(self) -> None:
        plan = prepare_order(
            self.record,
            make_state(self.proposal),
            LIMITS,
            metrics=metrics_for("US.AAPL", self.instrument),
        )
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(plan["tool"], "sim_trade_input_order")
        self.assertEqual(plan["proposal_hash"], self.proposal.canonical_hash())
        self.assertEqual(
            plan["arguments"],
            {
                "acc_id": "5829804",
                "market": 100,
                "symbol": "US.AAPL",
                "order_type": 1,
                "order_side": 1,
                "qty": "14",
                "price": "231.4000",
                "text": "qdr:P-20261118-1445-AAPL-01",
            },
        )

    def test_instrument_embedded_in_record_is_used(self) -> None:
        plan = prepare_order(
            record_for(self.proposal, self.instrument),
            make_state(self.proposal),
            LIMITS,
        )
        self.assertEqual(plan["status"], "READY")

    def test_missing_approval_is_rejected_with_r11(self) -> None:
        state = replace(make_state(self.proposal), approval=None)
        plan = prepare_order(
            self.record, state, LIMITS, metrics=metrics_for("US.AAPL", self.instrument)
        )
        self.assertEqual(plan["status"], "REJECTED")
        self.assertEqual(plan["gate"]["codes"], ["R11_NO_APPROVAL"])
        self.assertEqual(plan["reason"], "R11_NO_APPROVAL")
        self.assertNotIn("arguments", plan)

    def test_expired_approval_is_rejected(self) -> None:
        state = replace(
            make_state(self.proposal),
            now=NOW + LIMITS.approval_ttl + timedelta(minutes=1),
        )
        plan = prepare_order(
            self.record, state, LIMITS, metrics=metrics_for("US.AAPL", self.instrument)
        )
        self.assertEqual(plan["gate"]["codes"], ["R11_NO_APPROVAL"])

    def test_sub_dollar_metrics_are_rejected_with_r2(self) -> None:
        cheap = make_instrument(last_price=0.8)
        plan = prepare_order(
            self.record, make_state(self.proposal), LIMITS, metrics=metrics_for("US.AAPL", cheap)
        )
        self.assertEqual(plan["status"], "REJECTED")
        self.assertIn("R2_SIZE", plan["gate"]["codes"])

    def test_no_instrument_facts_at_all_raises(self) -> None:
        with self.assertRaises(ValueError):
            prepare_order(self.record, make_state(self.proposal), LIMITS)

    def test_metrics_defaults_are_applied(self) -> None:
        instrument = instrument_from_metrics(
            "US.AAPL", {"symbols": {"US.AAPL": {"last_price": 10.0, "market_cap": 1e9}}}
        )
        self.assertEqual(instrument.instrument_type, "stock")
        self.assertTrue(instrument.us_listed)
        self.assertTrue(instrument.tradable)


class TestRecordSubmission(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "guard.jsonl"
        self.plan = {
            "status": "READY",
            "ticket_id": "P-20261118-1445-AAPL-01",
            "arguments": {
                "acc_id": "5829804",
                "market": 100,
                "symbol": "US.AAPL",
                "order_type": 1,
                "order_side": 1,
                "qty": "14",
                "price": "231.4000",
                "text": "qdr:P-20261118-1445-AAPL-01",
            },
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_acknowledged_submission_is_logged(self) -> None:
        log = EventLog(self.path)
        outcome = record_submission(self.plan, {"ret_code": 0, "data": {"order_id": "42"}}, log)
        self.assertEqual(outcome, {"status": "SUBMITTED", "order_id": "42"})
        state = replay(self.path)
        self.assertEqual(len(state.submissions), 1)
        submitted = state.submissions[0]
        self.assertEqual(submitted.order_id, "42")
        self.assertEqual(submitted.quantity, 14)
        self.assertEqual(submitted.limit_price, 231.4)

    def test_broker_rejection_is_logged_without_halt(self) -> None:
        log = EventLog(self.path)
        outcome = record_submission(
            self.plan, {"ret_code": 8, "ret_msg": "rejected by broker"}, log
        )
        self.assertEqual(outcome["status"], "REJECTED")
        self.assertIn("rejected by broker", outcome["reason"])
        events = read_events(self.path)
        self.assertEqual(events[-1]["event"], "ORDER_REJECTED")
        self.assertFalse(replay(self.path).halted)

    def test_missing_order_id_halts(self) -> None:
        log = EventLog(self.path)
        outcome = record_submission(self.plan, {"ret_code": 0, "data": {}}, log)
        self.assertEqual(outcome, {"status": "UNKNOWN"})
        events = read_events(self.path)
        self.assertEqual([e["event"] for e in events], ["ORDER_UNKNOWN", "HALTED"])
        self.assertTrue(replay(self.path).halted)

    def test_malformed_result_halts(self) -> None:
        log = EventLog(self.path)
        self.assertEqual(record_submission(self.plan, None, log), {"status": "UNKNOWN"})
        self.assertTrue(replay(self.path).halted)

    def test_not_ready_plan_is_refused(self) -> None:
        log = EventLog(self.path)
        with self.assertRaises(ValueError):
            record_submission({"status": "REJECTED"}, {"ret_code": 0}, log)
        self.assertEqual(read_events(self.path), [])


class TestOtherEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "guard.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reconciliation_records_unfilled(self) -> None:
        log = EventLog(self.path)
        result = record_reconciliation(log, "O-1", 100, 60, "PARTIAL", 60)
        self.assertEqual(result["unfilled"], 40)
        state = replay(self.path)
        self.assertEqual(len(state.reconciliations), 1)
        self.assertEqual(state.reconciliations[0]["position_quantity"], 60)

    def test_kill_switch_and_resume_latch(self) -> None:
        log = EventLog(self.path)
        record_kill_switch(log, "operator", cancelled=("O-1",), failed=("O-2",))
        path_state = replay(self.path)
        self.assertTrue(path_state.kill_switch_engaged)
        record_resume(log, "reconciled with broker")
        self.assertEqual(read_events(self.path)[-1]["event"], "RESUME")

    def test_halt_flag_survives_replay(self) -> None:
        record_halt(EventLog(self.path), "unconfirmed broker outcome")
        self.assertTrue(replay(self.path).halted)

    def test_append_mode_continues_sequence(self) -> None:
        first = EventLog(self.path)
        first.emit("ORDER_SUBMITTED", ticket_id="T-1")
        second = EventLog(self.path, append=True)
        second.emit("ORDER_SUBMITTED", ticket_id="T-2")
        events = read_events(self.path)
        self.assertEqual([e["seq"] for e in events], [0, 1])
        self.assertEqual([e["ticket_id"] for e in events], ["T-1", "T-2"])

    def test_approval_for_missing_ticket_is_none(self) -> None:
        self.assertIsNone(approval_for({"ticket_id": "T-9"}, {}))

    def test_approval_for_round_trips(self) -> None:
        proposal = make_proposal()
        approval = approve(proposal)
        payload = {
            proposal.ticket_id: {
                "proposal_hash": approval.proposal_hash,
                "approved_at": approval.approved_at.isoformat(),
                "expires_at": None,
            }
        }
        loaded = approval_for({"ticket_id": proposal.ticket_id}, payload)
        self.assertEqual(loaded, approval)


if __name__ == "__main__":
    unittest.main()
