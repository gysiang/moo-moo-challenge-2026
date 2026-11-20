from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dashboard import state
from dashboard.cloud import CloudClient, CloudError
from dashboard.manual import ManualActions, normalize_account, atomic_json
from dashboard.server import App, serve
from guard.events import flow_state

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def result_fixture(request_id, mode="refresh", now=NOW):
    stamp = now.isoformat()
    raw = {"as_of": stamp, "symbols": {
        s: {"instrument_type": "etf", "market_cap": 1e12, "as_of": stamp,
            "closes": [70.5 + i * .5 for i in range(60)], "highs": [71 + i * .5 for i in range(60)]}
        for s in ("US.SPY", "US.QQQ")}}
    return {"version": 1, "request_id": request_id, "mode": mode, "account_id": "5829804", "market": 100,
            "as_of": stamp, "cash": {"ret_code": 0, "data": {"balance": "99000", "total_asset": "100000"}},
            "positions": {"ret_code": 0, "data": {"positions": [{"market": 100, "pstn_type": 0,
                "symbol": "AAPL", "qty": "10", "mv": "1000", "cost_price": "99"}]}},
            "order_pages": [{"ret_code": 0, "data": {"orders": [{"order_id": "test-old-order", "side": 1,
                "qty": "10", "cum_qty": "10", "status": 4, "symbol": "AAPL", "price": "100",
                "avg_fill_price": "99", "create_time": str(int((now - timedelta(days=1)).timestamp() * 1e6))}]},
                "pagination": {"has_more": False, "next_key": "0"}}],
            "market_raw": raw, "news": {s: [{"id": "N-" + s, "as_of": stamp, "source": "fixture"}]
                                       for s in raw["symbols"]}}


class FakeCloud:
    def __init__(self, clock=lambda: NOW):
        self.clock = clock
        self.created, self.sent = [], []
        self.artifacts = {}
        self.fail_send = False
        self.protocol = True
        self.deliver = True

    def guard_batch_ready(self, template_id):
        return self.protocol

    def create_forward_session(self, identity_id, template_id, title):
        session = {"id": f"sess_fixture{len(self.created) + 1}"}
        self.created.append(session)
        return session

    def send_forward_message(self, session_id, text):
        payload = json.loads(text)
        self.sent.append(payload)
        if self.fail_send:
            raise CloudError(0, "fixture uncertain delivery")
        if payload["kind"] == "dashboard-request":
            name = f"dashboard-{payload['request_id']}.json"
            self.artifacts[session_id] = (name, result_fixture(payload["request_id"], payload["mode"], self.clock()))
        else:
            name = f"guard-batch-{payload['batch_id']}.json"
            quantities = {r["ticket_id"]: r["quantity"] for cycle in payload["cycles"].values() for r in cycle["records"]}
            outcomes = [{"ticket_id": t["ticket_id"], "status": "RECONCILED", "order_id": "test-" + str(i),
                         "submitted": quantities[t["ticket_id"]], "filled": quantities[t["ticket_id"]], "unfilled": 0}
                        for i, t in enumerate(payload["tickets"])]
            self.artifacts[session_id] = (name, {"version": 1, "batch_id": payload["batch_id"],
                                               "account_id": "5829804", "halted": False, "outcomes": outcomes})
        return {"data": []}

    def files(self, *, scope_id, **kwargs):
        if not self.deliver or scope_id not in self.artifacts:
            return []
        name, payload = self.artifacts[scope_id]
        return [{"id": "file_" + scope_id, "filename": name, "scope": {"id": scope_id}}]

    def download_file(self, file_id):
        return json.dumps(self.artifacts[file_id.removeprefix("file_")][1]).encode()

    def forward_session(self, session_id):
        return {"id": session_id, "status": "idle"}

    def sessions(self, **kwargs):
        return [{**s, "title": "SYNTHETIC VERIFICATION — no broker", "agent": {"name": "Fixture"},
                 "status": "idle", "created_at": self.clock().isoformat()} for s in self.created]


class FakeGuard:
    session_id = "sess_fixtureGuard"


def initialize(runtime, now=NOW):
    atomic_json(runtime / "account-5829804.json", {"account_id": "5829804", "as_of": now.isoformat(),
                "equity": 100000, "cash": 99000, "day_open_equity": 100000, "baseline_verified": True,
                "positions": {}, "working_orders": [], "orders": [], "orders_complete": True, "executed_orders_today": 0})
    (runtime / "guard-events.jsonl").write_text("", encoding="utf-8")
    atomic_json(runtime / "approvals.json", {})


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        initialize(self.runtime)
        self.cloud = FakeCloud()
        self.app = App(runtime=self.runtime, config_path=ROOT / "config.json", baseline=100000,
                       demo=False, cloud=self.cloud, guard=FakeGuard())
        for target in ("dashboard.manual.datetime", "dashboard.batch.datetime"):
            p = patch(target, Clock)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.temp.cleanup()

    def scout(self):
        self.assertTrue(self.app.manual.start("scout")["ok"])
        self.assertEqual(self.app.manual.poll()["status"], "completed")
        cycles = state.list_cycles(self.runtime)
        cycle = cycles[-1]
        choices = [{"ticket_id": r["ticket_id"], "cycle_file": cycle["file"], "proposal_hash": r["proposal_hash"]}
                   for r in cycle["payload"]["records"] if r["status"] == "AWAITING_APPROVAL"]
        self.assertEqual(len(choices), 2)
        return {"tickets": choices}

    def test_startup_poll_does_not_create_research(self):
        self.app.poll()
        self.assertEqual(self.cloud.created, [])

    def test_refresh_imports_positions_and_fills_without_tickets(self):
        self.assertTrue(self.app.manual.start("refresh")["ok"])
        self.app.poll()
        s = self.app.state()
        self.assertEqual(s["manual_action"]["status"], "completed")
        self.assertEqual(s["positions"][0]["quantity"], 10)
        self.assertEqual(s["filled_orders"][0]["order_id"], "test-old-order")
        self.assertEqual(s["account_as_of"], NOW.isoformat())
        self.assertEqual(s["decisions"], [])
        self.assertEqual(state.read_json(self.runtime / "approvals.json"), {})

    def test_scout_uses_deterministic_gate_and_does_not_approve(self):
        self.scout()
        self.assertEqual(state.read_json(self.runtime / "approvals.json"), {})
        self.assertTrue(all(d["gate_codes"] == ["R11_NO_APPROVAL"] for d in self.app.state()["decisions"]))

    def test_closed_market_scout_has_no_tickets(self):
        with patch("dashboard.manual.market_session", return_value={"state": "CLOSED"}):
            self.app.manual.start("scout")
            self.app.manual.poll()
        self.assertEqual(self.app.state()["decisions"], [])

    def test_active_and_unknown_actions_never_resubmit(self):
        self.cloud.fail_send = True
        self.assertFalse(self.app.manual.start("refresh")["ok"])
        self.assertEqual(self.app.manual.status()["status"], "unknown")
        self.assertFalse(self.app.manual.start("scout")["ok"])
        reopened = ManualActions(self.cloud, self.runtime, ROOT / "config.json")
        self.assertFalse(reopened.start("refresh")["ok"])
        self.assertEqual(len(self.cloud.sent), 1)

    def test_corrupt_action_journal_fails_closed(self):
        self.app.manual.path.write_text("{", encoding="utf-8")
        self.assertFalse(self.app.manual.start("refresh")["ok"])
        self.assertEqual(self.cloud.created, [])

    def test_bad_result_preserves_previous_snapshot(self):
        before = (self.runtime / "account-5829804.json").read_bytes()
        run = self.app.manual.start("refresh")
        self.cloud.artifacts[run["session_id"]][1]["account_id"] = "wrong"
        self.assertEqual(self.app.manual.poll()["status"], "failed")
        self.assertEqual((self.runtime / "account-5829804.json").read_bytes(), before)

    def test_malformed_artifact_fails_without_stopping_polling(self):
        run = self.app.manual.start("refresh")
        self.cloud.artifacts[run["session_id"]][1]["as_of"] = 123
        self.assertEqual(self.app.manual.poll()["status"], "failed")
        self.assertTrue(self.app.manual.start("refresh")["ok"])
        self.assertEqual(self.app.manual.poll()["status"], "completed")

    def test_incomplete_history_is_not_empty_orders(self):
        payload = result_fixture("x")
        payload["order_pages"][0]["pagination"]["has_more"] = True
        with self.assertRaisesRegex(ValueError, "pagination"):
            normalize_account(payload, {})

    def test_partial_orders_show_both_working_and_fills(self):
        payload = result_fixture("x")
        row = payload["order_pages"][0]["data"]["orders"][0]
        row.update(status=3, cum_qty="4")
        account = normalize_account(payload, {})
        self.assertEqual(account["working_orders"][0]["filled_quantity"], 4)
        self.assertEqual(len(state.open_orders(account, [])), 1)

    def test_old_baseline_not_carried_to_next_day(self):
        payload = result_fixture("x", now=NOW + timedelta(days=1))
        prior = state.read_json(self.runtime / "account-5829804.json")
        account = normalize_account(payload, prior)
        self.assertFalse(account["baseline_verified"])
        self.assertNotIn("day_open_equity", account)

    def test_uncommitted_scout_cycle_cannot_be_approved(self):
        self.app.manual.start("scout")
        write = atomic_json
        def interrupted_write(path, payload):
            if path.name == "dashboard-snapshot.json":
                raise OSError("fixture interrupted commit")
            write(path, payload)
        with patch("dashboard.manual.atomic_json", interrupted_write):
            self.assertEqual(self.app.manual.poll()["status"], "failed")
        cycle = state.list_cycles(self.runtime)[-1]
        choices = {"tickets": [{"ticket_id": r["ticket_id"], "cycle_file": cycle["file"],
                               "proposal_hash": r["proposal_hash"]} for r in cycle["payload"]["records"]]}
        self.assertIn("latest committed", self.app.batch.approve(choices, NOW)["error"])
        self.assertEqual(state.read_json(self.runtime / "approvals.json"), {})

    def test_refresh_retains_latest_scout_identity(self):
        self.scout()
        request_id = self.app.state()["scout_request_id"]
        self.app.manual.start("refresh")
        self.app.manual.poll()
        self.assertEqual(self.app.state()["scout_request_id"], request_id)
        self.assertEqual({d["request_id"] for d in self.app.state()["decisions"]}, {request_id})

    def test_stale_symbol_data_refuses_whole_bundle(self):
        run = self.app.manual.start("scout")
        self.cloud.artifacts[run["session_id"]][1]["market_raw"]["symbols"]["US.SPY"]["as_of"] = (NOW - timedelta(hours=2)).isoformat()
        self.assertEqual(self.app.manual.poll()["status"], "failed")
        self.assertEqual(state.list_cycles(self.runtime), [])

    def test_successful_result_is_idempotent_after_restart(self):
        run = self.app.manual.start("scout")
        self.app.manual.poll()
        atomic_json(self.app.manual.path, run)
        self.assertEqual(self.app.manual.poll()["status"], "completed")
        self.assertEqual(len(state.list_cycles(self.runtime)), 1)

    def test_selected_batch_sent_once_with_exact_approvals(self):
        choices = self.scout()
        result = self.app.batch.approve(choices, NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.cloud.sent[-1]["kind"], "guard-batch")
        approvals = state.read_json(self.runtime / "approvals.json")
        self.assertEqual(set(approvals), {t["ticket_id"] for t in choices["tickets"]})
        self.assertFalse(self.app.batch.approve(choices, NOW)["ok"])
        self.assertEqual(len(self.cloud.sent), 2)

    def test_halt_blocks_approval_writes_and_dispatch(self):
        choices = self.scout()
        (self.runtime / "guard-events.jsonl").write_text('{"event":"HALTED"}\n', encoding="utf-8")
        result = self.app.batch.approve(choices, NOW)
        self.assertIn("halted", result["error"])
        self.assertEqual(state.read_json(self.runtime / "approvals.json"), {})
        self.assertEqual(len(self.cloud.sent), 1)

    def test_missing_guard_protocol_blocks_approval_writes(self):
        choices = self.scout()
        self.cloud.protocol = False
        result = self.app.batch.approve(choices, NOW)
        self.assertIn("protocol", result["error"])
        self.assertEqual(state.read_json(self.runtime / "approvals.json"), {})

    def test_changed_hash_blocks_entire_batch(self):
        choices = self.scout()
        choices["tickets"][1]["proposal_hash"] = "changed"
        self.assertFalse(self.app.batch.approve(choices, NOW)["ok"])
        self.assertEqual(state.read_json(self.runtime / "approvals.json"), {})

    def test_combined_cash_failure_does_not_partially_approve(self):
        choices = self.scout()
        path = self.runtime / "dashboard-snapshot.json"
        snapshot = state.read_json(path)
        snapshot["account"]["cash"] = 11000
        atomic_json(path, snapshot)
        self.assertFalse(self.app.batch.approve(choices, NOW)["ok"])
        self.assertEqual(state.read_json(self.runtime / "approvals.json"), {})

    def test_corrupt_batch_journal_fails_closed(self):
        choices = self.scout()
        self.app.batch.path.write_text("{", encoding="utf-8")
        self.assertFalse(self.app.batch.approve(choices, NOW)["ok"])

    def test_unknown_batch_send_stays_blocked_after_restart(self):
        choices = self.scout()
        self.cloud.fail_send = True
        result = self.app.batch.approve(choices, NOW)
        self.assertEqual(result["status"], "unknown")
        second = App(runtime=self.runtime, config_path=ROOT / "config.json", baseline=100000,
                     demo=False, cloud=self.cloud, guard=FakeGuard())
        self.assertFalse(second.batch.approve(choices, NOW)["ok"])
        self.assertEqual(len(self.cloud.sent), 2)

    def test_malformed_batch_artifact_keeps_approval_blocked(self):
        choices = self.scout()
        batch = self.app.batch.approve(choices, NOW)
        name, _ = self.cloud.artifacts[batch["session_id"]]
        self.cloud.artifacts[batch["session_id"]] = (name, [])
        result = self.app.batch.poll()
        self.assertIn("invalid", result["error"])
        self.assertFalse(self.app.batch.approve(choices, NOW)["ok"])

    def test_result_reconciliation_requires_valid_numbers(self):
        choices = self.scout()
        batch = self.app.batch.approve(choices, NOW)
        self.cloud.artifacts[batch["session_id"]][1]["outcomes"][0]["unfilled"] = 999
        self.assertNotEqual(self.app.batch.poll()["status"], "completed")

    def test_result_reconciliation_must_match_approved_quantity(self):
        choices = self.scout()
        batch = self.app.batch.approve(choices, NOW)
        outcome = self.cloud.artifacts[batch["session_id"]][1]["outcomes"][0]
        outcome.update(submitted=1, filled=1, unfilled=0)
        self.assertNotEqual(self.app.batch.poll()["status"], "completed")
        self.assertFalse(self.app.batch.approve(choices, NOW)["ok"])

    def test_unknown_result_latches_halt_once(self):
        choices = self.scout()
        batch = self.app.batch.approve(choices, NOW)
        result = self.cloud.artifacts[batch["session_id"]][1]
        result["halted"] = True
        result["outcomes"][0]["status"] = "UNKNOWN"
        self.app.batch.poll()
        before = (self.runtime / "guard-events.jsonl").read_bytes()
        self.app.batch.poll()
        self.assertEqual((self.runtime / "guard-events.jsonl").read_bytes(), before)
        self.assertTrue(flow_state(self.runtime / "guard-events.jsonl")["halted"])

    def test_completed_batch_requires_new_broker_refresh(self):
        choices = self.scout()
        self.app.batch.approve(choices, NOW)
        self.assertEqual(self.app.batch.poll()["status"], "completed")
        self.assertIn("Refresh broker", self.app.batch.approve(choices, NOW)["error"])

    def test_readiness_transport_never_sends_a_write(self):
        calls = []
        def transport(request, timeout):
            calls.append(request)
            return 200, json.dumps({"status": "active", "system": "legacy one ticket", "tools": []}).encode()
        client = CloudClient("fixture-secret", transport=transport)
        self.assertFalse(client.guard_batch_ready("tmpl_fixture"))
        self.assertEqual(calls[0].method, "GET")

    def test_malformed_template_readiness_fails_closed(self):
        for item in ([], None, {"status": "active", "system": None},
                     {"status": "active", "system": "guard-batch One approved batch runs serially", "tools": None}):
            with self.subTest(item=item):
                client = CloudClient("fixture-secret", transport=lambda request, timeout: (200, json.dumps({"item": item}).encode()))
                self.assertFalse(client.guard_batch_ready("tmpl_fixture"))

    def test_forward_create_uses_forward_body_and_typed_event(self):
        calls = []
        def transport(request, timeout):
            calls.append(request)
            return 200, json.dumps({"id": "sess_fixture"}).encode()
        client = CloudClient("fixture-secret", transport=transport)
        created = client.create_forward_session("idn_fixture", "tmpl_fixture", "test")
        client.send_forward_message(created["id"], "read only")
        self.assertTrue(all("/api/v1/forward/" in r.full_url for r in calls))
        self.assertNotIn("agent", json.loads(calls[0].data))
        self.assertEqual(json.loads(calls[1].data)["events"][0]["type"], "user.message")


def serve_fixture():
    from contextlib import ExitStack
    from dashboard.export import export_site
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from functools import partial
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        runtime = Path(tmp)
        now = datetime.now(timezone.utc)
        initialize(runtime, now)
        cloud = FakeCloud(lambda: datetime.now(timezone.utc))
        def open_market(now):
            return {"state": "OPEN", "closes_at": (now + timedelta(hours=3)).isoformat(),
                    "reason": "synthetic verification", "trading_day": True}
        for target in ("dashboard.manual.market_session", "dashboard.batch.market_session", "dashboard.state.market_session"):
            stack.enter_context(patch(target, open_market))
        httpd = serve(runtime=runtime, config_path=ROOT / "config.json", baseline=100000,
                      demo=False, cloud=cloud, guard=FakeGuard(), port=8767)
        app = httpd.RequestHandlerClass.app
        original = app._state
        def test_state():
            return {**original(), "verification_fixture": True}
        app._state = test_state
        app.batch.check_protocol()
        stopped = threading.Event()
        def poll():
            while not stopped.wait(.5):
                app.poll()
        threading.Thread(target=poll, daemon=True).start()
        export_site(runtime=runtime, config_path=ROOT / "config.json", baseline=100000,
                    out_dir=runtime / "static-export", demo=False)
        static = ThreadingHTTPServer(("127.0.0.1", 8768), partial(SimpleHTTPRequestHandler, directory=str(runtime / "static-export")))
        threading.Thread(target=static.serve_forever, daemon=True).start()
        print("Synthetic verification on 8767; static export on 8768; no broker or credentials.", flush=True)
        try:
            httpd.serve_forever()
        finally:
            stopped.set()
            static.shutdown()
            static.server_close()
            httpd.server_close()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve_fixture()
    else:
        unittest.main()
