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


if __name__ == "__main__":
    unittest.main()
