from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard import cloud, guard, state
from gate import EXIT_REASONS, ExitMandate, save_mandate

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "runtime" / "dashboard-demo"
NOW = datetime(2026, 11, 18, 20, 30, tzinfo=timezone.utc)
TOKEN = "secret-token-123"


class FakeTransport:
    def __init__(self, respond):
        self.calls = []
        self._respond = respond

    def __call__(self, request, timeout):
        self.calls.append(request)
        return self._respond(request)


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name) / "runtime"
        shutil.copytree(DEMO, self.runtime)
        self.approvals = "approvals-demo.json"
        self.stamp(NOW - timedelta(minutes=5))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def stamp(self, moment: datetime) -> None:
        stamp = moment.isoformat()
        cycle_path = self.runtime / "cycle-20261118-0945.json"
        cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        cycle["generated_at"] = stamp
        cycle_path.write_text(json.dumps(cycle, indent=2), encoding="utf-8")
        for name in ("account-5829804.json", "metrics.json"):
            path = self.runtime / name
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["as_of"] = stamp
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def approve(self, ticket="P-20261118-0945-SPY-01", ttl=30):
        return state.approve_ticket(
            runtime=self.runtime,
            cycle_file="cycle-20261118-0945.json",
            ticket_id=ticket,
            ttl_minutes=ttl,
            now=NOW,
            approvals_file=self.approvals,
            rejections_file="rejections-demo.jsonl",
        )

    def envelope(
        self,
        ticket="P-20261118-0945-SPY-01",
        now=NOW,
        max_age=guard.MAX_SNAPSHOT_AGE_MINUTES,
    ):
        return guard.build_envelope(
            runtime=self.runtime,
            cycle_file="cycle-20261118-0945.json",
            ticket_id=ticket,
            approvals_file=self.approvals,
            now=now,
            max_snapshot_age_minutes=max_age,
        )


class EnvelopeTests(RuntimeCase):
    def test_bundle_carries_cycle_account_metrics_and_approval(self):
        self.assertTrue(self.approve()["ok"])
        envelope = self.envelope()
        self.assertEqual(envelope["kind"], "guard-trigger")
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["ticket_id"], "P-20261118-0945-SPY-01")
        self.assertEqual(envelope["cycle_file"], "runtime/cycle-20261118-0945.json")
        self.assertEqual(envelope["requested_by"], "dashboard-operator")
        evidence = envelope["evidence"]
        self.assertEqual(set(evidence), {"cycle", "account", "metrics", "approvals"})
        self.assertEqual(evidence["account"]["account_id"], "5829804")
        self.assertIn("US.SPY", evidence["metrics"]["symbols"])
        approval = evidence["approvals"]["P-20261118-0945-SPY-01"]
        record = next(
            r
            for r in evidence["cycle"]["records"]
            if r["ticket_id"] == "P-20261118-0945-SPY-01"
        )
        self.assertEqual(approval["proposal_hash"], record["proposal_hash"])

    def test_missing_approval_is_refused(self):
        with self.assertRaises(guard.GuardLinkError):
            self.envelope()

    def test_expired_approval_is_refused(self):
        self.assertTrue(self.approve(ttl=30)["ok"])
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope(now=NOW + timedelta(hours=1), max_age=120)
        self.assertIn("expired", str(caught.exception))

    def test_rejected_gate_ticket_without_approval_is_refused(self):
        with self.assertRaises(guard.GuardLinkError):
            self.envelope(ticket="P-20261118-0945-IWM-03")

    def test_invalid_ids_and_paths_are_refused(self):
        self.assertTrue(self.approve()["ok"])
        with self.assertRaises(guard.GuardLinkError):
            guard.build_envelope(
                runtime=self.runtime,
                cycle_file="../config.json",
                ticket_id="P-20261118-0945-SPY-01",
                approvals_file=self.approvals,
                now=NOW,
            )
        with self.assertRaises(guard.GuardLinkError):
            guard.build_envelope(
                runtime=self.runtime,
                cycle_file="cycle-20261118-0945.json",
                ticket_id="../../etc/passwd",
                approvals_file=self.approvals,
                now=NOW,
            )

    def test_missing_metrics_is_refused(self):
        self.assertTrue(self.approve()["ok"])
        (self.runtime / "metrics.json").unlink()
        with self.assertRaises(guard.GuardLinkError):
            self.envelope()

    def test_missing_account_snapshot_is_refused(self):
        self.assertTrue(self.approve()["ok"])
        (self.runtime / "account-5829804.json").unlink()
        with self.assertRaises(guard.GuardLinkError):
            self.envelope()


class SnapshotFreshnessTests(RuntimeCase):
    def test_stale_cycle_is_refused(self):
        self.assertTrue(self.approve()["ok"])
        self.stamp(NOW - timedelta(hours=2))
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope()
        self.assertIn("cycle snapshot is 120 min old", str(caught.exception))

    def test_stale_metrics_are_refused(self):
        self.assertTrue(self.approve()["ok"])
        path = self.runtime / "metrics.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["as_of"] = (NOW - timedelta(hours=3)).isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope()
        self.assertIn("metrics snapshot", str(caught.exception))

    def test_stale_account_is_refused(self):
        self.assertTrue(self.approve()["ok"])
        path = self.runtime / "account-5829804.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["as_of"] = (NOW - timedelta(hours=3)).isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope()
        self.assertIn("account snapshot", str(caught.exception))

    def test_missing_timestamp_is_refused(self):
        self.assertTrue(self.approve()["ok"])
        path = self.runtime / "metrics.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["as_of"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope()
        self.assertIn("no timestamp", str(caught.exception))

    def test_future_timestamp_is_refused(self):
        self.assertTrue(self.approve()["ok"])
        self.stamp(NOW + timedelta(hours=1))
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope()
        self.assertIn("future", str(caught.exception))

    def test_configured_max_age_is_honored(self):
        self.assertTrue(self.approve()["ok"])
        self.stamp(NOW - timedelta(hours=2))
        envelope = self.envelope(max_age=180)
        self.assertEqual(envelope["ticket_id"], "P-20261118-0945-SPY-01")


class MandateEnvelopeTests(RuntimeCase):
    EXIT_TICKET = "P-20261118-0945-NVDA-02"

    def grant(self, **overrides) -> None:
        mandate = ExitMandate(
            mandate_id=overrides.get("mandate_id", "M-DEMO-1"),
            account_id=overrides.get("account_id", "5829804"),
            granted_at=overrides.get("granted_at", NOW - timedelta(hours=1)),
            expires_at=overrides.get("expires_at", NOW + timedelta(hours=12)),
            allowed_reasons=frozenset(EXIT_REASONS),
            max_exits_per_run=overrides.get("max_exits_per_run", 6),
            note="demo mandate",
        )
        save_mandate(self.runtime / "exit-mandate.json", mandate)

    def test_mandated_exit_needs_no_approval(self):
        self.grant()
        envelope = self.envelope(ticket=self.EXIT_TICKET)
        self.assertEqual(envelope["authorization"], "MANDATE")
        self.assertEqual(envelope["mandate_id"], "M-DEMO-1")
        self.assertEqual(envelope["evidence"]["approvals"], {})

    def test_approved_ticket_still_uses_approval_authorization(self):
        self.grant()
        self.assertTrue(self.approve()["ok"])
        envelope = self.envelope()
        self.assertEqual(envelope["authorization"], "APPROVAL")
        self.assertNotIn("mandate_id", envelope)

    def test_mandate_does_not_cover_entries(self):
        self.grant()
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope()
        self.assertIn("no approval record", str(caught.exception))

    def test_partial_exit_is_not_covered(self):
        self.grant()
        path = self.runtime / "cycle-20261118-0945.json"
        cycle = json.loads(path.read_text(encoding="utf-8"))
        for record in cycle["records"]:
            if record["ticket_id"] == self.EXIT_TICKET:
                record["quantity"] = 10
        path.write_text(json.dumps(cycle), encoding="utf-8")
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope(ticket=self.EXIT_TICKET)
        self.assertIn("full exits only", str(caught.exception))

    def test_expired_mandate_is_refused(self):
        self.grant(expires_at=NOW - timedelta(minutes=1))
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope(ticket=self.EXIT_TICKET)
        self.assertIn("mandate expired", str(caught.exception))

    def test_other_account_mandate_is_refused(self):
        self.grant(account_id="9999999")
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope(ticket=self.EXIT_TICKET)
        self.assertIn("mandate is for account", str(caught.exception))

    def test_tampered_mandate_is_refused(self):
        self.grant()
        path = self.runtime / "exit-mandate.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["max_exits_per_run"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope(ticket=self.EXIT_TICKET)
        self.assertIn("hash mismatch", str(caught.exception))

    def test_no_mandate_file_means_exit_needs_approval(self):
        with self.assertRaises(guard.GuardLinkError) as caught:
            self.envelope(ticket=self.EXIT_TICKET)
        self.assertIn("no approval record", str(caught.exception))


class GuardLinkTests(unittest.TestCase):
    def _link(self, respond):
        transport = FakeTransport(respond)
        client = cloud.CloudClient(TOKEN, transport=transport)
        return guard.GuardLink(client, "sess_guardtest01"), transport

    def test_trigger_posts_user_message_with_envelope(self):
        link, transport = self._link(lambda request: (200, b'{"data": []}'))
        envelope = {"kind": "guard-trigger", "version": 1, "ticket_id": "P-00000000-0000-SPY-01"}
        outcome = link.trigger(envelope)
        self.assertTrue(outcome["triggered"])
        self.assertEqual(outcome["session_id"], "sess_guardtest01")
        request = transport.calls[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://api.qoder.com/api/v1/cloud/sessions/sess_guardtest01/events",
        )
        text = json.loads(request.data.decode("utf-8"))["events"][0]["content"][0]["text"]
        self.assertIn("GUARD TRIGGER", text)
        self.assertIn('"kind": "guard-trigger"', text)
        self.assertNotIn(TOKEN, text)

    def test_cloud_error_returns_status_without_raising(self):
        link, _ = self._link(
            lambda request: (401, b'{"error":{"message":"PAT invalid or expired"}}')
        )
        outcome = link.trigger({"kind": "guard-trigger"})
        self.assertFalse(outcome["triggered"])
        self.assertEqual(outcome["status"], 401)
        self.assertIn("PAT invalid", outcome["error"])
        self.assertNotIn(TOKEN, outcome["error"])

    def test_invalid_session_id_is_rejected_at_construction(self):
        client = cloud.CloudClient(TOKEN, transport=FakeTransport(lambda request: (200, b"{}")))
        with self.assertRaises(ValueError):
            guard.GuardLink(client, "../../agents")


if __name__ == "__main__":
    unittest.main()
