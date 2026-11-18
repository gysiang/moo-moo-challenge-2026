from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard import cloud, guard, state

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

    def tearDown(self) -> None:
        self._tmp.cleanup()

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

    def envelope(self, ticket="P-20261118-0945-SPY-01", now=NOW):
        return guard.build_envelope(
            runtime=self.runtime,
            cycle_file="cycle-20261118-0945.json",
            ticket_id=ticket,
            approvals_file=self.approvals,
            now=now,
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
        with self.assertRaises(guard.GuardLinkError):
            self.envelope(now=NOW + timedelta(hours=1))

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
