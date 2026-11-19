from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard import state
from dashboard.server import serve
from gate import EXIT_REASONS, ExitMandate, save_mandate

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "runtime" / "dashboard-demo"
CONFIG = ROOT / "config.json"
NOW = datetime(2026, 11, 18, 20, 30, tzinfo=timezone.utc)


class DemoRuntime:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "runtime"
        shutil.copytree(DEMO, self.path)

    def build(self, **overrides) -> dict:
        kwargs = dict(
            runtime=self.path,
            config_path=CONFIG,
            baseline=100000.0,
            now=NOW,
            demo=True,
            approvals_file="approvals-demo.json",
            events_file="guard-events-demo.jsonl",
            history_file="equity-history-demo.jsonl",
            rejections_file="rejections-demo.jsonl",
            triggers_file="guard-triggers-demo.jsonl",
        )
        kwargs.update(overrides)
        return state.build_state(**kwargs)

    def close(self) -> None:
        self._tmp.cleanup()


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = DemoRuntime()

    def tearDown(self) -> None:
        self.rt.close()

    def test_state_sections(self) -> None:
        payload = self.rt.build()
        self.assertTrue(payload["has_snapshot"])
        self.assertEqual(payload["account_id"], "5829804")
        self.assertEqual(len(payload["positions"]), 3)
        self.assertEqual(payload["kpi"]["positions_used"], 3)
        self.assertEqual(payload["kpi"]["orders_used"], 2)
        self.assertAlmostEqual(payload["kpi"]["equity"], 102150.0)
        self.assertGreater(payload["kpi"]["total_pnl"], 0)

    def test_missing_snapshot_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = state.build_state(
                runtime=Path(tmp), config_path=CONFIG, baseline=100000.0, now=NOW
            )
        self.assertFalse(payload["has_snapshot"])
        self.assertIsNone(payload["account_id"])
        self.assertEqual(payload["decisions"], [])

    def test_position_pnl_and_weight(self) -> None:
        rows = {row["symbol"]: row for row in self.rt.build()["positions"]}
        spy = rows["US.SPY"]
        self.assertEqual(spy["quantity"], 13)
        self.assertAlmostEqual(spy["market_value"], 9521.72)
        self.assertAlmostEqual(spy["unrealized"], 13 * 732.44 - 13 * 735.2, places=2)
        self.assertLess(spy["unrealized"], 0)
        self.assertGreater(spy["weight"], 0.09)

    def test_drawdown_is_negative_from_peak(self) -> None:
        payload = self.rt.build()
        self.assertLess(payload["kpi"]["drawdown_pct"], 0)
        self.assertAlmostEqual(payload["kpi"]["peak_equity"], 102380.5)
        self.assertLessEqual(payload["kpi"]["max_drawdown_pct"], payload["kpi"]["drawdown_pct"])

    def test_decisions_and_lifecycle(self) -> None:
        payload = self.rt.build()
        rows = {row["ticket_id"]: row for row in payload["decisions"]}
        self.assertEqual(rows["P-20261118-0945-SPY-01"]["status"], "AWAITING_APPROVAL")
        self.assertEqual(rows["P-20261118-0945-MSFT-04"]["status"], "APPROVED")
        self.assertEqual(rows["P-20261118-0945-IWM-03"]["status"], "REJECTED")
        self.assertEqual(rows["P-20261118-0945-IWM-03"]["gate_codes"], ["R10_EVIDENCE_STALE"])
        self.assertEqual(payload["counts"]["awaiting"], 2)

    def test_expired_approval_is_not_approved(self) -> None:
        payload = self.rt.build(now=NOW + timedelta(hours=2))
        rows = {row["ticket_id"]: row for row in payload["decisions"]}
        self.assertEqual(rows["P-20261118-0945-MSFT-04"]["status"], "APPROVAL_EXPIRED")

    def test_open_orders_merge_submissions_and_working(self) -> None:
        rows = self.rt.build()["open_orders"]
        self.assertEqual(len(rows), 1)
        submitted = rows[0]
        self.assertEqual(submitted["kind"], "SUBMITTED")
        self.assertEqual(submitted["order_id"], "9102")

    def test_working_orders_render_when_log_has_no_submission(self) -> None:
        path = self.rt.path / "guard-events-demo.jsonl"
        kept = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if '"order_id": "9101"' in line
        ]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        rows = self.rt.build()["open_orders"]
        self.assertEqual([row["kind"] for row in rows], ["WORKING"])
        self.assertEqual(rows[0]["symbol"], "US.AMD")

    def test_equity_series_appends_only_on_change(self) -> None:
        first = self.rt.build()
        second = self.rt.build()
        self.assertEqual(len(first["equity_series"]), len(second["equity_series"]))
        history = (self.rt.path / "equity-history-demo.jsonl").read_text(encoding="utf-8")
        self.assertEqual(history.count("\n"), 10)


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = DemoRuntime()

    def tearDown(self) -> None:
        self.rt.close()

    def test_approve_writes_hash_bound_record(self) -> None:
        result = state.approve_ticket(
            runtime=self.rt.path,
            cycle_file="cycle-20261118-0945.json",
            ticket_id="P-20261118-0945-SPY-01",
            ttl_minutes=30,
            now=NOW,
            approvals_file="approvals-demo.json",
        )
        self.assertTrue(result["ok"])
        saved = json.loads((self.rt.path / "approvals-demo.json").read_text(encoding="utf-8"))
        self.assertIn("P-20261118-0945-SPY-01", saved)
        self.assertEqual(
            saved["P-20261118-0945-SPY-01"]["proposal_hash"],
            "b1c9f0d4e8a24d9f7b6c3a1e5d8f2b4c7a9e0d1f3b5c7a9e2d4f6b8c0a1e3d5f",
        )
        payload = self.rt.build()
        rows = {row["ticket_id"]: row for row in payload["decisions"]}
        self.assertEqual(rows["P-20261118-0945-SPY-01"]["status"], "APPROVED")

    def test_reject_non_awaiting_ticket(self) -> None:
        result = state.approve_ticket(
            runtime=self.rt.path,
            cycle_file="cycle-20261118-0945.json",
            ticket_id="P-20261118-0945-IWM-03",
            ttl_minutes=30,
            now=NOW,
            approvals_file="approvals-demo.json",
        )
        self.assertFalse(result["ok"])
        self.assertIn("AWAITING_APPROVAL", result["error"])

    def test_unknown_ticket_and_cycle(self) -> None:
        bad_ticket = state.approve_ticket(
            runtime=self.rt.path,
            cycle_file="cycle-20261118-0945.json",
            ticket_id="P-20261118-0945-ZZZ-99",
            ttl_minutes=30,
            now=NOW,
            approvals_file="approvals-demo.json",
        )
        self.assertFalse(bad_ticket["ok"])
        bad_cycle = state.approve_ticket(
            runtime=self.rt.path,
            cycle_file="../escape.json",
            ticket_id="P-20261118-0945-SPY-01",
            ttl_minutes=30,
            now=NOW,
            approvals_file="approvals-demo.json",
        )
        self.assertFalse(bad_cycle["ok"])


class RejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = DemoRuntime()

    def tearDown(self) -> None:
        self.rt.close()

    def _reject(self, ticket="P-20261118-0945-SPY-01", reason="too risky"):
        return state.reject_ticket(
            runtime=self.rt.path,
            cycle_file="cycle-20261118-0945.json",
            ticket_id=ticket,
            reason=reason,
            now=NOW,
            approvals_file="approvals-demo.json",
            rejections_file="rejections-demo.jsonl",
        )

    def test_reject_appends_log_and_overlays_status(self):
        result = self._reject()
        self.assertTrue(result["ok"])
        lines = (self.rt.path / "rejections-demo.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["ticket_id"], "P-20261118-0945-SPY-01")
        self.assertEqual(row["reason"], "too risky")
        self.assertEqual(
            row["proposal_hash"],
            "b1c9f0d4e8a24d9f7b6c3a1e5d8f2b4c7a9e0d1f3b5c7a9e2d4f6b8c0a1e3d5f",
        )
        payload = self.rt.build()
        rows = {row["ticket_id"]: row for row in payload["decisions"]}
        self.assertEqual(rows["P-20261118-0945-SPY-01"]["status"], "REJECTED_BY_OPERATOR")
        self.assertEqual(rows["P-20261118-0945-SPY-01"]["rejection_reason"], "too risky")
        self.assertEqual(payload["counts"]["operator_rejected"], 1)

    def test_reject_is_idempotent(self):
        self._reject()
        again = self._reject()
        self.assertTrue(again["ok"])
        self.assertTrue(again.get("already_rejected"))
        lines = (self.rt.path / "rejections-demo.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_reject_refuses_non_awaiting_ticket(self):
        result = self._reject(ticket="P-20261118-0945-IWM-03")
        self.assertFalse(result["ok"])
        self.assertIn("AWAITING_APPROVAL", result["error"])

    def test_reject_refuses_after_live_approval(self):
        state.approve_ticket(
            runtime=self.rt.path,
            cycle_file="cycle-20261118-0945.json",
            ticket_id="P-20261118-0945-SPY-01",
            ttl_minutes=30,
            now=NOW,
            approvals_file="approvals-demo.json",
            rejections_file="rejections-demo.jsonl",
        )
        result = self._reject()
        self.assertFalse(result["ok"])
        self.assertIn("live approval", result["error"])

    def test_approve_refuses_after_rejection(self):
        self._reject()
        result = state.approve_ticket(
            runtime=self.rt.path,
            cycle_file="cycle-20261118-0945.json",
            ticket_id="P-20261118-0945-SPY-01",
            ttl_minutes=30,
            now=NOW,
            approvals_file="approvals-demo.json",
            rejections_file="rejections-demo.jsonl",
        )
        self.assertFalse(result["ok"])
        self.assertIn("rejected", result["error"])

    def test_trigger_states_surface_in_decisions(self):
        state.append_jsonl(
            self.rt.path / "guard-triggers-demo.jsonl",
            {
                "ticket_id": "P-20261118-0945-SPY-01",
                "state": "accepted",
                "requested_at": "2026-11-18T20:31:00+00:00",
                "session_id": "sess_guardtest01",
            },
        )
        state.append_jsonl(
            self.rt.path / "guard-triggers-demo.jsonl",
            {
                "ticket_id": "P-20261118-0945-MSFT-04",
                "state": "unknown",
                "requested_at": "2026-11-18T20:32:00+00:00",
                "session_id": "sess_guardtest01",
            },
        )
        rows = {row["ticket_id"]: row for row in self.rt.build()["decisions"]}
        self.assertEqual(rows["P-20261118-0945-SPY-01"]["guard_triggered_at"], "2026-11-18T20:31:00+00:00")
        self.assertEqual(rows["P-20261118-0945-SPY-01"]["guard_state"], "accepted")
        self.assertIsNone(rows["P-20261118-0945-MSFT-04"]["guard_triggered_at"])
        self.assertEqual(rows["P-20261118-0945-MSFT-04"]["guard_state"], "unknown")


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = DemoRuntime()
        self.httpd = serve(
            runtime=self.rt.path,
            config_path=CONFIG,
            baseline=100000.0,
            demo=True,
            port=0,
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.rt.close()

    def test_state_endpoint(self) -> None:
        with urlopen(f"http://127.0.0.1:{self.port}/api/state", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["demo"])
        self.assertEqual(len(payload["decisions"]), 4)

    def test_approve_endpoint(self) -> None:
        body = json.dumps(
            {"ticket_id": "P-20261118-0945-SPY-01", "cycle_file": "cycle-20261118-0945.json"}
        ).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{self.port}/api/approve",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])

    def test_non_loopback_host_is_rejected(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/api/state", headers={"Host": "evil.example"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        connection.close()

    def test_path_traversal_is_blocked(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            urlopen(
                f"http://127.0.0.1:{self.port}/static/..%2F..%2Fconfig.json",
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 404)


class FakeGuard:
    session_id = "sess_guardtest01"

    def __init__(self):
        self.envelopes: list[dict] = []
        self.results: list[dict] = []

    def trigger(self, envelope: dict) -> dict:
        self.envelopes.append(envelope)
        if self.results:
            return self.results.pop(0)
        return {"triggered": True, "session_id": self.session_id}


class LiveRuntime(DemoRuntime):
    """The demo fixtures under their production file names."""

    def __init__(self) -> None:
        super().__init__()
        for src, dst in (
            ("approvals-demo.json", "approvals.json"),
            ("guard-events-demo.jsonl", "guard-events.jsonl"),
            ("equity-history-demo.jsonl", "equity-history.jsonl"),
        ):
            shutil.copyfile(self.path / src, self.path / dst)

    def stamp(self, moment: datetime) -> None:
        stamp = moment.isoformat()
        cycle_path = self.path / "cycle-20261118-0945.json"
        cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        cycle["generated_at"] = stamp
        cycle_path.write_text(json.dumps(cycle, indent=2), encoding="utf-8")
        for name in ("account-5829804.json", "metrics.json"):
            path = self.path / name
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["as_of"] = stamp
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def stamp_fresh(self) -> None:
        self.stamp(datetime.now(timezone.utc) - timedelta(minutes=5))


class GuardLinkServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = LiveRuntime()
        self.rt.stamp_fresh()
        self.guard = FakeGuard()
        self.httpd = serve(
            runtime=self.rt.path,
            config_path=CONFIG,
            baseline=100000.0,
            demo=False,
            guard=self.guard,
            port=0,
        )
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.rt.close()

    def _post(self, route: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"http://127.0.0.1:{self.port}{route}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def _state(self) -> dict:
        with urlopen(f"http://127.0.0.1:{self.port}/api/state", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _approve(self, ticket="P-20261118-0945-SPY-01"):
        return self._post(
            "/api/approve",
            {"ticket_id": ticket, "cycle_file": "cycle-20261118-0945.json"},
        )

    def test_approve_starts_guard_once(self):
        status, body = self._approve()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["guard"]["triggered"])
        self.assertEqual(self.guard.session_id, body["guard"]["session_id"])
        self.assertEqual(len(self.guard.envelopes), 1)
        envelope = self.guard.envelopes[0]
        self.assertEqual(envelope["kind"], "guard-trigger")
        self.assertEqual(envelope["ticket_id"], "P-20261118-0945-SPY-01")
        self.assertEqual(envelope["cycle_file"], "runtime/cycle-20261118-0945.json")
        self.assertEqual(
            envelope["evidence"]["approvals"]["P-20261118-0945-SPY-01"]["proposal_hash"],
            "b1c9f0d4e8a24d9f7b6c3a1e5d8f2b4c7a9e0d1f3b5c7a9e2d4f6b8c0a1e3d5f",
        )
        rows = {row["ticket_id"]: row for row in self._state()["decisions"]}
        self.assertIsNotNone(rows["P-20261118-0945-SPY-01"]["guard_triggered_at"])

        second = self._approve()[1]
        self.assertIn("already exists", second["guard"]["error"])
        self.assertEqual(len(self.guard.envelopes), 1)

    def test_http_refusal_allows_retry(self):
        self.guard.results = [
            {
                "triggered": False,
                "session_id": self.guard.session_id,
                "status": 401,
                "error": "PAT invalid",
            }
        ]
        status, body = self._approve()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["guard"]["triggered"])
        self.assertEqual(body["guard"]["state"], "rejected")
        retry = self._approve()[1]
        self.assertTrue(retry["guard"]["triggered"])
        self.assertEqual(len(self.guard.envelopes), 2)

    def test_unknown_send_is_not_retried(self):
        self.guard.results = [
            {
                "triggered": False,
                "session_id": self.guard.session_id,
                "status": 0,
                "error": "cloud API unreachable",
            }
        ]
        self._approve()
        second = self._approve()[1]
        self.assertIn("already exists", second["guard"]["error"])
        self.assertEqual(len(self.guard.envelopes), 1)
        rows = {row["ticket_id"]: row for row in self._state()["decisions"]}
        self.assertEqual(rows["P-20261118-0945-SPY-01"]["guard_state"], "unknown")
        self.assertIsNone(rows["P-20261118-0945-SPY-01"]["guard_triggered_at"])

    def _grant_mandate(self, **overrides) -> None:
        now = datetime.now(timezone.utc)
        save_mandate(
            self.rt.path / "exit-mandate.json",
            ExitMandate(
                mandate_id="M-TEST-1",
                account_id="5829804",
                granted_at=now - timedelta(hours=1),
                expires_at=now + timedelta(hours=12),
                allowed_reasons=frozenset(EXIT_REASONS),
                max_exits_per_run=overrides.get("max_exits_per_run", 6),
                note="test mandate",
            ),
        )

    def _exit_ticket(self, **overrides) -> tuple[int, dict]:
        return self._post(
            "/api/approve",
            {
                "ticket_id": "P-20261118-0945-NVDA-02",
                "cycle_file": "cycle-20261118-0945.json",
                **overrides,
            },
        )

    def test_stale_snapshot_blocks_the_guard_trigger(self):
        self.rt.stamp(datetime.now(timezone.utc) - timedelta(hours=3))
        status, body = self._approve()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["guard"]["triggered"])
        self.assertIn("snapshot is", body["guard"]["error"])
        self.assertEqual(self.guard.envelopes, [])

    def test_mandated_exit_runs_guard_without_an_approval(self):
        self._grant_mandate()
        status, body = self._exit_ticket()
        self.assertEqual(status, 200)
        self.assertEqual(body["authorization"], "MANDATE")
        self.assertTrue(body["guard"]["triggered"])
        envelope = self.guard.envelopes[0]
        self.assertEqual(envelope["authorization"], "MANDATE")
        self.assertEqual(envelope["mandate_id"], "M-TEST-1")
        self.assertEqual(envelope["evidence"]["approvals"], {})
        approvals = json.loads((self.rt.path / "approvals.json").read_text(encoding="utf-8"))
        self.assertNotIn("P-20261118-0945-NVDA-02", approvals)

    def test_mandate_summary_is_published_in_state(self):
        self._grant_mandate()
        mandate = self._state()["exit_mandate"]
        self.assertTrue(mandate["active"])
        self.assertEqual(mandate["mandate_id"], "M-TEST-1")
        self.assertEqual(mandate["max_exits_per_run"], 6)
        self.assertNotIn("note", mandate)

    def test_partial_exit_is_refused_under_a_mandate(self):
        self._grant_mandate()
        path = self.rt.path / "cycle-20261118-0945.json"
        cycle = json.loads(path.read_text(encoding="utf-8"))
        for record in cycle["records"]:
            if record["ticket_id"] == "P-20261118-0945-NVDA-02":
                record["quantity"] = 10
        path.write_text(json.dumps(cycle), encoding="utf-8")
        status, body = self._exit_ticket()
        self.assertEqual(status, 409)
        self.assertIn("full exits only", body["error"])
        self.assertEqual(self.guard.envelopes, [])

    def test_gate_rejected_exit_is_refused_under_a_mandate(self):
        self._grant_mandate()
        path = self.rt.path / "cycle-20261118-0945.json"
        cycle = json.loads(path.read_text(encoding="utf-8"))
        for record in cycle["records"]:
            if record["ticket_id"] == "P-20261118-0945-NVDA-02":
                record["status"] = "REJECTED"
        path.write_text(json.dumps(cycle), encoding="utf-8")
        status, body = self._exit_ticket()
        self.assertEqual(status, 409)
        self.assertIn("rejected by the gate", body["error"])

    def test_mandate_does_not_authorize_entries(self):
        self._grant_mandate()
        status, body = self._approve()
        self.assertEqual(status, 200)
        self.assertNotIn("authorization", body)
        self.assertTrue(body["guard"]["triggered"])
        self.assertEqual(self.guard.envelopes[0]["authorization"], "APPROVAL")

    def test_reject_endpoint_writes_log_and_blocks_approval(self):
        status, body = self._post(
            "/api/reject",
            {
                "ticket_id": "P-20261118-0945-SPY-01",
                "cycle_file": "cycle-20261118-0945.json",
                "reason": "spread too wide",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        row = json.loads(
            (self.rt.path / "rejections.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertEqual(row["reason"], "spread too wide")
        decisions = {row["ticket_id"]: row for row in self._state()["decisions"]}
        self.assertEqual(decisions["P-20261118-0945-SPY-01"]["status"], "REJECTED_BY_OPERATOR")

        status, body = self._approve()
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertEqual(self.guard.envelopes, [])

    def test_reject_requires_fields(self):
        status, body = self._post("/api/reject", {"ticket_id": "P-20261118-0945-SPY-01"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class DemoGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = DemoRuntime()
        self.guard = FakeGuard()
        self.httpd = serve(
            runtime=self.rt.path,
            config_path=CONFIG,
            baseline=100000.0,
            demo=True,
            guard=self.guard,
            port=0,
        )
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.rt.close()

    def test_demo_approval_never_triggers_guard(self):
        request = Request(
            f"http://127.0.0.1:{self.port}/api/approve",
            data=json.dumps(
                {"ticket_id": "P-20261118-0945-SPY-01", "cycle_file": "cycle-20261118-0945.json"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        self.assertTrue(body["ok"])
        self.assertFalse(body["guard"]["triggered"])
        self.assertFalse(body["guard"]["configured"])
        self.assertEqual(self.guard.envelopes, [])
        self.assertFalse((self.rt.path / "guard-triggers-demo.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
