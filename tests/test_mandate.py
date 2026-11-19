from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from gate import (
    EXIT_REASONS,
    AccountState,
    Evidence,
    ExitMandate,
    Position,
    Proposal,
    evaluate,
    load_mandate,
    save_mandate,
)
from guard import ACCEPTED, BLOCKED, REJECTED, UNKNOWN, EventLog, Guard, read_events
from guard.bridge import prepare_order, record_submission
from guard.bridge_cli import main as bridge_main
from guard.mandate_cli import main as mandate_main
from scout.models import AWAITING_APPROVAL, READY
from tests.test_bridge import metrics_for, record_for
from tests.test_gate import (
    LIMITS,
    NOW,
    fresh_quote,
    make_instrument,
    make_proposal,
    make_state,
)
from tests.test_guard import FakeBroker
from tests.test_scout import (
    NOW as SCOUT_NOW,
    SETTINGS,
    WATCHLIST,
    fresh_news,
    make_account,
    make_metrics,
    make_snapshot,
)
from scout.cycle import Scout
from scout.cli import main as scout_main
from tests.test_scout import CONFIG

WATCHLIST_FILE = Path(__file__).resolve().parent.parent / "scout" / "watchlist.json"

ACCOUNT = "5829804"
HELD = Position("US.AAPL", 14, 3_239.60, 240.0)


def make_mandate(**overrides) -> ExitMandate:
    base = ExitMandate(
        mandate_id="M-TEST-1",
        account_id=ACCOUNT,
        granted_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        allowed_reasons=frozenset(EXIT_REASONS),
        max_exits_per_run=6,
        note="operator pre-authorization for risk-reducing exits",
    )
    return replace(base, **overrides)


def make_exit(**overrides) -> Proposal:
    base = make_proposal(
        side="SELL",
        exit_reason="EXIT_BELOW_SMA50",
        evidence=(fresh_quote(),),
    )
    return replace(base, **overrides)


def scout_mandate(**overrides) -> ExitMandate:
    return make_mandate(
        granted_at=SCOUT_NOW - timedelta(hours=1),
        expires_at=SCOUT_NOW + timedelta(hours=12),
        **overrides,
    )


def exit_state(proposal: Proposal, **overrides) -> AccountState:
    kwargs = {
        "positions": {"US.AAPL": HELD},
        "approval": None,
        "account_id": ACCOUNT,
    }
    kwargs.update(overrides)
    return make_state(proposal, **kwargs)


class TestMandateModel(unittest.TestCase):
    def test_rejects_unknown_reason(self) -> None:
        with self.assertRaises(ValueError):
            make_mandate(allowed_reasons=frozenset({"EXIT_MOON"}))

    def test_rejects_empty_reasons(self) -> None:
        with self.assertRaises(ValueError):
            make_mandate(allowed_reasons=frozenset())

    def test_rejects_expiry_not_after_grant(self) -> None:
        with self.assertRaises(ValueError):
            make_mandate(expires_at=NOW - timedelta(hours=2))

    def test_rejects_non_positive_cap(self) -> None:
        with self.assertRaises(ValueError):
            make_mandate(max_exits_per_run=0)

    def test_requires_account_and_mandate_ids(self) -> None:
        with self.assertRaises(ValueError):
            make_mandate(account_id="")
        with self.assertRaises(ValueError):
            make_mandate(mandate_id="")

    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mandate.json"
            mandate = make_mandate()
            save_mandate(path, mandate)
            self.assertEqual(load_mandate(path), mandate)

    def test_missing_file_is_no_mandate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_mandate(Path(tmp) / "absent.json"))

    def test_edited_file_fails_hash_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mandate.json"
            save_mandate(path, make_mandate())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["max_exits_per_run"] = 99
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_mandate(path)


class TestMandateGate(unittest.TestCase):
    def test_mandated_full_exit_passes_without_approval(self) -> None:
        proposal = make_exit()
        result = evaluate(
            proposal, make_instrument(), exit_state(proposal), LIMITS, make_mandate()
        )
        self.assertTrue(result.passed, result.rejections)

    def test_buy_still_requires_approval(self) -> None:
        proposal = make_proposal()
        state = make_state(proposal, approval=None, account_id=ACCOUNT)
        result = evaluate(proposal, make_instrument(), state, LIMITS, make_mandate())
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_sell_without_exit_reason_still_requires_approval(self) -> None:
        proposal = make_exit(exit_reason="")
        result = evaluate(
            proposal, make_instrument(), exit_state(proposal), LIMITS, make_mandate()
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_partial_exit_is_not_covered(self) -> None:
        proposal = make_exit(quantity=7, est_cost=1_619.80)
        result = evaluate(
            proposal, make_instrument(), exit_state(proposal), LIMITS, make_mandate()
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_other_account_is_not_covered(self) -> None:
        proposal = make_exit()
        result = evaluate(
            proposal,
            make_instrument(),
            exit_state(proposal, account_id="9999999"),
            LIMITS,
            make_mandate(),
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_unset_account_is_not_covered(self) -> None:
        proposal = make_exit()
        result = evaluate(
            proposal,
            make_instrument(),
            exit_state(proposal, account_id=""),
            LIMITS,
            make_mandate(),
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_expired_mandate_is_not_covered(self) -> None:
        proposal = make_exit()
        mandate = make_mandate(expires_at=NOW - timedelta(minutes=1))
        result = evaluate(proposal, make_instrument(), exit_state(proposal), LIMITS, mandate)
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_future_mandate_is_not_covered(self) -> None:
        proposal = make_exit()
        mandate = make_mandate(
            granted_at=NOW + timedelta(hours=1), expires_at=NOW + timedelta(hours=2)
        )
        result = evaluate(proposal, make_instrument(), exit_state(proposal), LIMITS, mandate)
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_unmandated_reason_is_not_covered(self) -> None:
        proposal = make_exit(exit_reason="EXIT_STOP_LOSS")
        mandate = make_mandate(allowed_reasons=frozenset({"EXIT_BELOW_SMA50"}))
        result = evaluate(proposal, make_instrument(), exit_state(proposal), LIMITS, mandate)
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_run_cap_is_enforced(self) -> None:
        proposal = make_exit()
        state = exit_state(proposal, mandated_exits_this_run=2)
        mandate = make_mandate(max_exits_per_run=2)
        result = evaluate(proposal, make_instrument(), state, LIMITS, mandate)
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_mandate_does_not_bypass_evidence_rule(self) -> None:
        proposal = make_exit(
            evidence=(
                Evidence("E1", "quote", NOW - timedelta(hours=30), "moomoo"),
            )
        )
        result = evaluate(
            proposal, make_instrument(), exit_state(proposal), LIMITS, make_mandate()
        )
        self.assertIn("R10_EVIDENCE_STALE", result.codes)

    def test_mandate_does_not_bypass_inventory_rule(self) -> None:
        proposal = make_exit()
        state = exit_state(proposal, positions={})
        result = evaluate(proposal, make_instrument(), state, LIMITS, make_mandate())
        self.assertIn("R4_FUNDS", result.codes)

    def test_without_mandate_exit_awaits_approval(self) -> None:
        proposal = make_exit()
        result = evaluate(proposal, make_instrument(), exit_state(proposal), LIMITS)
        self.assertEqual(result.codes, ("R11_NO_APPROVAL",))


class TestScoutMandate(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = make_snapshot(
            **{
                "US.AAPL": make_metrics("US.AAPL", 90.0, 98.0, 95.0, 101.0),
                "US.MSFT": make_metrics("US.MSFT", 90.0, 99.0, 95.0, 102.0),
            }
        )
        self.account = replace(
            make_account(
                positions={
                    "US.AAPL": Position("US.AAPL", 10, 900.0, 100.0),
                    "US.MSFT": Position("US.MSFT", 10, 900.0, 100.0),
                }
            ),
            account_id=ACCOUNT,
        )

    def run_cycle(self, mandate: ExitMandate | None):
        scout = Scout(LIMITS, SETTINGS, WATCHLIST, mandate)
        return scout.run(
            slot="15:30",
            now=SCOUT_NOW,
            snapshot=self.snapshot,
            account=self.account,
        )

    def test_exit_only_run_is_ready_without_approval(self) -> None:
        report = self.run_cycle(scout_mandate())
        self.assertFalse(report.entries_allowed)
        self.assertEqual(len(report.records), 2)
        for record in report.records:
            self.assertEqual(record.status, READY, record.gate.rejections)
            self.assertEqual(record.proposal.side, "SELL")
            self.assertTrue(record.proposal.exit_reason)

    def test_report_carries_mandate_and_exit_reason(self) -> None:
        payload = self.run_cycle(scout_mandate()).to_dict()
        self.assertEqual(payload["exit_mandate"]["mandate_id"], "M-TEST-1")
        self.assertEqual(payload["exit_mandate"]["max_exits_per_run"], 6)
        for record in payload["records"]:
            self.assertEqual(record["exit_reason"], "EXIT_BELOW_SMA50")

    def test_without_mandate_exits_await_approval(self) -> None:
        report = self.run_cycle(None)
        self.assertEqual({r.status for r in report.records}, {AWAITING_APPROVAL})
        self.assertIsNone(report.to_dict()["exit_mandate"])

    def test_run_cap_leaves_extra_exits_awaiting_approval(self) -> None:
        report = self.run_cycle(scout_mandate(max_exits_per_run=1))
        statuses = {r.proposal.symbol: r.status for r in report.records}
        self.assertEqual(statuses["US.AAPL"], READY)
        self.assertEqual(statuses["US.MSFT"], AWAITING_APPROVAL)

    def test_entries_still_await_approval_in_entry_slot(self) -> None:
        scout = Scout(LIMITS, SETTINGS, WATCHLIST, scout_mandate())
        snapshot = make_snapshot()
        report = scout.run(
            slot="09:45",
            now=SCOUT_NOW,
            snapshot=snapshot,
            account=replace(make_account(), account_id=ACCOUNT),
            news={symbol: fresh_news(symbol) for symbol in snapshot.metrics},
        )
        buys = [r for r in report.records if r.proposal.side == "BUY"]
        self.assertTrue(buys)
        for record in buys:
            self.assertEqual(record.status, AWAITING_APPROVAL)


class TestGuardMandate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "guard.jsonl"
        self.log = EventLog(self.log_path)

    def make_guard(self, mandate: ExitMandate | None) -> Guard:
        return Guard(FakeBroker(), LIMITS, self.log, mandate)

    def test_mandated_exit_is_accepted_without_approval(self) -> None:
        proposal = make_exit()
        guard = self.make_guard(make_mandate())
        result = guard.submit(proposal, make_instrument(), exit_state(proposal))
        self.assertEqual(result.status, ACCEPTED, result.reason)
        self.assertFalse(guard.halted)

    def test_expired_entry_approval_rejects_without_halting_exits(self) -> None:
        guard = self.make_guard(make_mandate())
        entry = make_proposal(ticket_id="P-ENTRY-01")
        expired = replace(
            make_state(entry, account_id=ACCOUNT).approval,
            expires_at=NOW - timedelta(minutes=1),
        )
        blocked = guard.submit(
            entry, make_instrument(), make_state(entry, account_id=ACCOUNT, approval=expired)
        )
        self.assertEqual(blocked.status, REJECTED)
        self.assertFalse(guard.halted)

        exit_proposal = make_exit(ticket_id="P-EXIT-01")
        result = guard.submit(
            exit_proposal, make_instrument(), exit_state(exit_proposal)
        )
        self.assertEqual(result.status, ACCEPTED, result.reason)

    def test_cap_is_enforced_across_submits(self) -> None:
        guard = self.make_guard(make_mandate(max_exits_per_run=1))
        first = make_exit(ticket_id="P-EXIT-01")
        second = make_exit(ticket_id="P-EXIT-02")
        self.assertEqual(
            guard.submit(first, make_instrument(), exit_state(first)).status, ACCEPTED
        )
        again = guard.submit(second, make_instrument(), exit_state(second))
        self.assertEqual(again.status, REJECTED)
        self.assertIn("R11_NO_APPROVAL", again.gate.codes)

    def test_unknown_outcome_halts_mandated_exits_too(self) -> None:
        broker = FakeBroker()
        guard = Guard(broker, LIMITS, self.log, make_mandate())
        broker.fail_next_submit = True
        first = make_exit(ticket_id="P-EXIT-01")
        self.assertEqual(
            guard.submit(first, make_instrument(), exit_state(first)).status, UNKNOWN
        )
        self.assertTrue(guard.halted)
        second = make_exit(ticket_id="P-EXIT-02")
        self.assertEqual(
            guard.submit(second, make_instrument(), exit_state(second)).status, BLOCKED
        )

    def test_submission_logs_exit_reason(self) -> None:
        proposal = make_exit()
        guard = self.make_guard(make_mandate())
        guard.submit(proposal, make_instrument(), exit_state(proposal))
        event = read_events(self.log_path)[-1]
        self.assertEqual(event["event"], "ORDER_SUBMITTED")
        self.assertEqual(event["exit_reason"], "EXIT_BELOW_SMA50")


class TestBridgeMandate(unittest.TestCase):
    def setUp(self) -> None:
        self.proposal = make_exit()
        self.instrument = make_instrument()
        self.record = record_for(self.proposal, self.instrument)
        self.state = exit_state(self.proposal)

    def prepare(self, **kwargs):
        return prepare_order(self.record, self.state, LIMITS, **kwargs)

    def test_mandated_exit_is_ready_and_labelled(self) -> None:
        plan = self.prepare(mandate=make_mandate(), run_key="2026-11-18:15:30")
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(plan["authorization"], "MANDATE")
        self.assertEqual(plan["mandate_id"], "M-TEST-1")
        self.assertEqual(plan["arguments"]["order_side"], 2)
        self.assertEqual(plan["run_key"], "2026-11-18:15:30")

    def test_without_mandate_exit_is_rejected(self) -> None:
        plan = self.prepare()
        self.assertEqual(plan["status"], "REJECTED")
        self.assertEqual(plan["reason"], "R11_NO_APPROVAL")

    def test_exits_already_used_in_run_block_the_mandate(self) -> None:
        plan = self.prepare(mandate=make_mandate(max_exits_per_run=1), exits_this_run=1)
        self.assertEqual(plan["status"], "REJECTED")
        self.assertEqual(plan["reason"], "R11_NO_APPROVAL")

    def test_submission_records_exit_reason_and_run_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(Path(tmp) / "guard.jsonl")
            plan = self.prepare(mandate=make_mandate(), run_key="2026-11-18:15:30")
            outcome = record_submission(
                plan, {"ret_code": 0, "data": {"order_id": "O-1"}}, log
            )
            self.assertEqual(outcome["status"], "SUBMITTED")
            event = read_events(Path(tmp) / "guard.jsonl")[-1]
            self.assertEqual(event["exit_reason"], "EXIT_BELOW_SMA50")
            self.assertEqual(event["run_key"], "2026-11-18:15:30")
            self.assertEqual(event["authorization"], "MANDATE")


class TestMandateCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "exit-mandate.json"
        self.log = Path(self.tmp.name) / "guard-events.jsonl"

    def grant(self, *extra: str) -> int:
        return mandate_main(
            [
                "grant",
                "--account-id",
                ACCOUNT,
                "--hours",
                "12",
                "--granted-by",
                "operator",
                "--note",
                "overnight stop protection",
                "--path",
                str(self.path),
                "--now",
                NOW.isoformat(),
                *extra,
            ]
        )

    def test_grant_show_revoke_roundtrip(self) -> None:
        self.assertEqual(self.grant(), 0)
        mandate = load_mandate(self.path)
        self.assertEqual(mandate.account_id, ACCOUNT)
        self.assertEqual(mandate.allowed_reasons, frozenset(EXIT_REASONS))
        self.assertEqual(
            mandate_main(["show", "--path", str(self.path), "--now", NOW.isoformat()]), 0
        )
        self.assertEqual(
            mandate_main(
                [
                    "revoke",
                    "--path",
                    str(self.path),
                    "--log",
                    str(self.log),
                    "--reason",
                    "competition week over",
                    "--now",
                    NOW.isoformat(),
                ]
            ),
            0,
        )
        self.assertIsNone(load_mandate(self.path))
        self.assertTrue(list(Path(self.tmp.name).glob("exit-mandate.revoked-*.json")))
        event = read_events(self.log)[-1]
        self.assertEqual(event["event"], "MANDATE_REVOKED")
        self.assertEqual(event["mandate_id"], mandate.mandate_id)

    def test_grant_refuses_to_replace_an_active_mandate(self) -> None:
        self.assertEqual(self.grant(), 0)
        with self.assertRaises(SystemExit):
            self.grant()
        self.assertEqual(self.grant("--replace"), 0)

    def test_grant_requires_note_and_operator(self) -> None:
        with self.assertRaises(SystemExit):
            mandate_main(
                [
                    "grant",
                    "--account-id",
                    ACCOUNT,
                    "--hours",
                    "12",
                    "--granted-by",
                    " ",
                    "--note",
                    "x",
                    "--path",
                    str(self.path),
                ]
            )

    def test_grant_rejects_unknown_reason(self) -> None:
        with self.assertRaises(SystemExit):
            self.grant("--reasons", "EXIT_BELOW_SMA50,EXIT_MOON")

    def test_grant_rejects_out_of_range_hours(self) -> None:
        with self.assertRaises(SystemExit):
            self.grant("--hours", "0")
        with self.assertRaises(SystemExit):
            self.grant("--hours", "999")

    def test_show_without_mandate_returns_one(self) -> None:
        self.assertEqual(
            mandate_main(["show", "--path", str(self.path), "--now", NOW.isoformat()]), 1
        )

    def test_show_reports_expired_mandate(self) -> None:
        self.assertEqual(self.grant(), 0)
        later = (NOW + timedelta(hours=13)).isoformat()
        self.assertEqual(
            mandate_main(["show", "--path", str(self.path), "--now", later]), 1
        )

    def test_tampered_mandate_blocks_show(self) -> None:
        self.assertEqual(self.grant(), 0)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["expires_at"] = (NOW + timedelta(days=30)).isoformat()
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(SystemExit):
            mandate_main(["show", "--path", str(self.path)])


class TestBridgeCliMandate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mandate_path = self.root / "exit-mandate.json"
        save_mandate(self.mandate_path, scout_mandate())
        snapshot = make_snapshot(
            **{"US.AAPL": make_metrics("US.AAPL", 90.0, 98.0, 95.0, 101.0)}
        )
        account = replace(
            make_account(positions={"US.AAPL": Position("US.AAPL", 10, 900.0, 100.0)}),
            account_id=ACCOUNT,
        )
        report = Scout(LIMITS, SETTINGS, WATCHLIST, scout_mandate()).run(
            slot="15:30", now=SCOUT_NOW, snapshot=snapshot, account=account
        )
        self.cycle_path = self.root / "cycle.json"
        self.cycle_path.write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
        self.ticket = report.records[0].proposal.ticket_id
        self.account_path = self.root / "account.json"
        self.account_path.write_text(
            json.dumps(
                {
                    "account_id": ACCOUNT,
                    "cash": 90_000.0,
                    "equity": 100_000.0,
                    "positions": {
                        "US.AAPL": {
                            "quantity": 10,
                            "market_value": 900.0,
                            "average_cost": 100.0,
                        }
                    },
                    "executed_orders_today": 0,
                }
            ),
            encoding="utf-8",
        )
        self.approvals_path = self.root / "approvals.json"
        self.approvals_path.write_text("{}", encoding="utf-8")
        self.metrics_path = self.root / "metrics.json"
        self.metrics_path.write_text(
            json.dumps(metrics_for("US.AAPL", make_instrument(last_price=90.0))),
            encoding="utf-8",
        )
        self.out = self.root / "order-plan.json"
        self.log = self.root / "guard-events.jsonl"

    def prepare(self, *extra: str) -> int:
        return bridge_main(
            [
                "prepare",
                "--cycle",
                str(self.cycle_path),
                "--ticket",
                self.ticket,
                "--account",
                str(self.account_path),
                "--approvals",
                str(self.approvals_path),
                "--metrics",
                str(self.metrics_path),
                "--mandate",
                str(self.mandate_path),
                "--log",
                str(self.log),
                "--out",
                str(self.out),
                "--now",
                SCOUT_NOW.isoformat(),
                *extra,
            ]
        )

    def test_exit_is_ready_without_any_approval(self) -> None:
        self.assertEqual(self.prepare(), 0)
        plan = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(plan["authorization"], "MANDATE")
        self.assertEqual(plan["arguments"]["order_side"], 2)
        self.assertEqual(plan["arguments"]["qty"], "10")

    def test_run_cap_counts_prior_submissions_in_the_same_run(self) -> None:
        save_mandate(self.mandate_path, scout_mandate(max_exits_per_run=1))
        self.assertEqual(self.prepare(), 0)
        plan = json.loads(self.out.read_text(encoding="utf-8"))
        record_submission(plan, {"ret_code": 0, "data": {"order_id": "O-1"}}, EventLog(self.log, append=True))
        second = self.prepare("--ticket", self.ticket)
        self.assertEqual(second, 2)
        blocked = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(blocked["status"], "REJECTED")
        self.assertEqual(blocked["reason"], "R11_NO_APPROVAL")

    def test_no_mandate_file_means_exit_needs_approval(self) -> None:
        self.assertEqual(self.prepare("--mandate", str(self.root / "absent.json")), 2)
        plan = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(plan["reason"], "R11_NO_APPROVAL")


class TestExitOnlyCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "market-raw.json"
        closes = [200.0 - i for i in range(60)]
        self.raw.write_text(
            json.dumps(
                {
                    "as_of": (SCOUT_NOW - timedelta(minutes=5)).isoformat(),
                    "symbols": {
                        "US.AAPL": {
                            "instrument_type": "stock",
                            "market_cap": 3.5e12,
                            "closes": closes,
                            "highs": [c + 1 for c in closes],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.account = self.root / "account.json"
        self.account.write_text(
            json.dumps(
                {
                    "account_id": ACCOUNT,
                    "as_of": (SCOUT_NOW - timedelta(minutes=5)).isoformat(),
                    "cash": 90_000.0,
                    "equity": 100_000.0,
                    "positions": {
                        "US.AAPL": {
                            "quantity": 10,
                            "market_value": 1_410.0,
                            "average_cost": 180.0,
                        }
                    },
                    "executed_orders_today": 0,
                }
            ),
            encoding="utf-8",
        )
        self.approvals = self.root / "approvals.json"
        self.approvals.write_text("{}", encoding="utf-8")
        self.mandate = self.root / "exit-mandate.json"
        save_mandate(self.mandate, scout_mandate())
        self.out = self.root / "cycle.json"

    def plan(self, *extra: str) -> int:
        return scout_main(
            [
                "plan",
                "--config",
                str(CONFIG),
                "--watchlist",
                str(WATCHLIST_FILE),
                "--raw",
                str(self.raw),
                "--account",
                str(self.account),
                "--approvals",
                str(self.approvals),
                "--mandate",
                str(self.mandate),
                "--out",
                str(self.out),
                "--now",
                SCOUT_NOW.isoformat(),
                *extra,
            ]
        )

    def test_exit_only_defaults_the_slot_to_now_and_blocks_entries(self) -> None:
        self.assertEqual(self.plan("--exit-only"), 0)
        payload = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(payload["slot"], "09:45")
        self.assertFalse(payload["entries_allowed"])
        self.assertEqual(payload["exit_mandate"]["mandate_id"], "M-TEST-1")
        self.assertTrue(payload["records"])
        for record in payload["records"]:
            self.assertEqual(record["side"], "SELL")
            self.assertEqual(record["status"], READY)
            self.assertEqual(record["exit_reason"], "EXIT_BELOW_SMA50")

    def test_exit_only_without_a_mandate_awaits_approval(self) -> None:
        self.assertEqual(
            self.plan("--exit-only", "--mandate", str(self.root / "absent.json")), 0
        )
        payload = json.loads(self.out.read_text(encoding="utf-8"))
        for record in payload["records"]:
            self.assertEqual(record["status"], AWAITING_APPROVAL)

    def test_slot_is_required_for_a_normal_cycle(self) -> None:
        with self.assertRaises(SystemExit):
            self.plan()


if __name__ == "__main__":
    unittest.main()