from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coordinator.__main__ import main
from coordinator.bundle import canonical, decode, main as bundle_main, make_bundle
from coordinator.cloud import ReadOnlyCloud, poll_once, readonly_transport
from coordinator.store import Store
from dashboard.cloud import CloudError
from gate.models import Evidence, Proposal

NOW = datetime(2026, 11, 20, 16, 5, tzinfo=timezone.utc)
AGENT = "agent_scout"
SID = "sess_scout"


def fixture(symbols=("US.AMZN",), costs=(3500,), *, now=NOW, slot="11:00", start_index=1):
    generated = now - timedelta(minutes=1)
    records = []
    metrics = {"as_of": (generated - timedelta(seconds=10)).isoformat(), "symbols": {}}
    for index, (symbol, cost) in enumerate(zip(symbols, costs), start_index):
        ticket = f"P-{generated:%Y%m%d}-{slot.replace(':', '')}-{symbol[3:]}-{index:02}"
        p = Proposal(ticket, generated, symbol, "BUY", 10, "LIMIT", cost / 10, cost, 1,
                     evidence=(Evidence("Q-1", "quote", generated, "fixture quote"),
                               Evidence("N-1", "news", generated, "fixture news")))
        records.append({"ticket_id": ticket, "proposal_hash": p.canonical_hash(), "symbol": symbol,
                        "side": "BUY", "quantity": 10, "order_type": "LIMIT", "limit_price": cost / 10,
                        "est_cost": cost, "created_at": generated.isoformat(), "sleeve": "satellite",
                        "status": "AWAITING_APPROVAL", "evidence": [
                            {"id": e.evidence_id, "kind": e.kind, "as_of": e.as_of.isoformat(), "source": e.source}
                            for e in p.evidence]})
        metrics["symbols"][symbol] = {"instrument_type": "stock", "last_price": cost / 10,
                                      "market_cap": 1_000_000_000}
    cycle = {"slot": slot, "generated_at": generated.isoformat(), "trading_day": True,
             "entries_allowed": True, "records": records}
    account = {"account_id": "5829804", "as_of": generated.isoformat(), "cash": 100000,
               "equity": 100000, "day_open_equity": 900000, "executed_orders_today": 0,
               "positions": {}, "working_orders": [], "note": "do not copy this private note"}
    return make_bundle(cycle, account, metrics)


def payload(bundle):
    return canonical(bundle).encode()


class CoordinatorFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "pilot.sqlite3"
        self.store = Store.initialize(self.db, pilot_id="pilot-test", starts_at=NOW - timedelta(minutes=10),
                                      scout_agent_id=AGENT)
        self.store.add_baseline("2026-11-20", 100000, NOW.replace(hour=14, minute=30), "fixture opening equity")

    def process(self, bundle=None, *, fid="file_a", now=NOW):
        return self.store.process(payload(bundle or fixture()), session_id=SID, file_id=fid, now=now)


class CoordinatorTests(CoordinatorFixture):
    def test_reservation_is_dry_run_not_approval(self):
        row = self.process()["decisions"][0]
        self.assertEqual(row["status"], "DRY_RUN_RESERVED")
        self.assertEqual(row["original_gate_codes"], ["R11_NO_APPROVAL"])
        self.assertEqual(row["execution_blockers"], ["TRADING_DISABLED", "GUARD_HALTED"])
        self.assertEqual((row["quantity"], row["limit_price"]), (10, 350))
        self.assertNotIn("arguments", row)
        self.assertFalse(self.store.status()["trading_enabled"])

    def test_two_buy_limit_includes_pending(self):
        result = self.process(fixture(("US.AMZN", "US.NVDA", "US.MSFT"), (3000, 3000, 3000)))
        self.assertEqual([r["status"] for r in result["decisions"]],
                         ["DRY_RUN_RESERVED", "DRY_RUN_RESERVED", "DRY_RUN_REJECTED"])
        self.assertIn("PILOT_BUY_COUNT", result["decisions"][2]["codes"])

    def test_ten_percent_budget_includes_pending(self):
        result = self.process(fixture(("US.AMZN", "US.NVDA"), (7000, 3500)))
        self.assertIn("PILOT_BUY_BUDGET", result["decisions"][1]["codes"])
        self.assertEqual(self.store.status()["pending_notional"], 7000)

    def test_budget_exact_boundary(self):
        result = self.process(fixture(("US.AMZN", "US.NVDA"), (7500, 2500)))
        self.assertTrue(all(r["status"] == "DRY_RUN_RESERVED" for r in result["decisions"]))
        self.assertEqual(self.store.status()["pending_notional"], 10000)

    def test_oversized_order_not_resized(self):
        row = self.process(fixture(costs=(10001,)))["decisions"][0]
        self.assertIn("PILOT_BUY_BUDGET", row["codes"])
        self.assertEqual((row["quantity"], row["limit_price"]), (10, 1000.1))
        self.assertEqual(self.store.status()["reservations"], 0)

    def test_restart_preserves_reservation_and_exact_expiry(self):
        self.process()
        old_status = self.store.status()
        self.store = Store(self.db)
        self.process(fixture(("US.NVDA",), (3500,)), fid="file_b")
        row = self.process(fixture(("US.MSFT",), (3500,)), fid="file_c")["decisions"][0]
        self.assertIn("PILOT_BUY_COUNT", row["codes"])
        self.assertEqual(old_status["expires_at"], self.store.status()["expires_at"])

    def test_reinitializing_existing_database_is_forbidden(self):
        with self.assertRaises(FileExistsError):
            Store.initialize(self.db, pilot_id="renewal", starts_at=NOW, scout_agent_id=AGENT)
        self.assertEqual(self.store.status()["pilot_id"], "pilot-test")

    def test_missing_database_does_not_create_empty_ledger(self):
        missing = Path(self.tmp.name) / "lost.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            Store(missing).status()
        self.assertFalse(missing.exists())

    def test_atomic_rollback_after_reservation_before_commit(self):
        with patch.object(Store, "_audit", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                self.process()
        self.assertEqual(self.store.status()["reservations"], 0)
        self.assertEqual(self.store.status()["artifacts"], 0)
        self.assertEqual(self.process()["decisions"][0]["status"], "DRY_RUN_RESERVED")

    def test_two_workers_cannot_exceed_daily_cap(self):
        symbols = ["US.AMZN", "US.NVDA", "US.MSFT", "US.AAPL", "US.COST", "US.PG"]
        barrier = threading.Barrier(len(symbols))
        def worker(index):
            barrier.wait(timeout=10)
            return Store(self.db).process(payload(fixture((symbols[index],), (3500,))),
                                          session_id=SID, file_id=f"file_{index}", now=NOW)
        with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
            results = list(pool.map(worker, range(len(symbols))))
        self.assertEqual(sum(r["decisions"][0]["status"] == "DRY_RUN_RESERVED" for r in results), 2)
        self.assertEqual(self.store.status()["pending_notional"], 7000)

    def test_duplicate_artifact_idempotent(self):
        first = self.process()
        self.assertEqual(first, self.process())
        self.assertEqual(self.store.status()["reservations"], 1)
        self.assertEqual(self.store.status()["artifacts"], 1)

    def test_duplicate_ticket_in_new_artifact_idempotent(self):
        self.process()
        result = self.process(fid="file_b")
        self.assertEqual(result["decisions"][0]["status"], "DRY_RUN_DUPLICATE")
        self.assertEqual(self.store.status()["reservations"], 1)

    def test_conflicting_artifact_identity_rejected(self):
        self.process()
        with self.assertRaisesRegex(ValueError, "identity collision"):
            self.process(fixture(costs=(3600,)))
        self.assertEqual(self.store.status()["pending_notional"], 3500)

    def test_changed_ticket_same_id_cannot_be_reused(self):
        self.process()
        result = self.process(fixture(costs=(3600,)), fid="file_b")
        self.assertIn("TICKET_IDENTITY_COLLISION", result["decisions"][0]["codes"])

    def test_same_symbol_new_ticket_is_still_duplicate(self):
        self.process()
        result = self.process(fixture(start_index=2), fid="file_b")
        self.assertIn("R9_DUPLICATE", result["decisions"][0]["codes"])

    def test_baseline_is_immutable_and_not_from_scout_account(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.add_baseline("2026-11-20", 900000, NOW.replace(hour=14, minute=30), "different baseline")
        bundle = fixture(costs=(15000,))
        bundle["account"]["day_open_equity"] = 900000
        self.assertIn("PILOT_BUY_BUDGET", self.process(bundle)["decisions"][0]["codes"])

    def test_missing_baseline_fails_closed(self):
        other = Store.initialize(Path(self.tmp.name) / "other.sqlite3", pilot_id="missing",
                                 starts_at=NOW - timedelta(minutes=10), scout_agent_id=AGENT)
        result = other.process(payload(fixture()), session_id=SID, file_id="file_a", now=NOW)
        self.assertIn("BASELINE_MISSING", result["decisions"][0]["codes"])
        self.assertEqual(other.status()["reservations"], 0)

    def test_revocation_persists_without_affecting_halt(self):
        self.store.revoke(NOW)
        self.store = Store(self.db)
        result = self.process()
        self.assertIn("PILOT_REVOKED", result["decisions"][0]["codes"])
        self.assertTrue(self.store.status()["guard_halted"])

    def test_expiry_is_exactly_twenty_four_hours_no_renewal(self):
        expiry = NOW - timedelta(minutes=10) + timedelta(hours=24)
        result = self.process(now=expiry)
        self.assertIn("PILOT_INACTIVE", result["decisions"][0]["codes"])
        self.assertEqual(self.store.status()["expires_at"], expiry.isoformat())

    def test_future_pilot_is_inactive(self):
        self.assertIn("PILOT_INACTIVE", self.process(now=NOW - timedelta(hours=1))["bundle_codes"])

    def test_stale_cycle_account_or_metrics_rejected(self):
        for section, field in (("cycle", "generated_at"), ("account", "as_of"), ("metrics", "as_of")):
            with self.subTest(section=section):
                b = fixture()
                b[section][field] = (NOW - timedelta(minutes=31)).isoformat()
                result = self.process(b, fid="file_" + section)
                self.assertIn("STALE_OR_FUTURE_SNAPSHOT", result["bundle_codes"])
        self.assertEqual(self.store.status()["reservations"], 0)

    def test_future_and_mixed_snapshots_rejected(self):
        b = fixture()
        b["account"]["as_of"] = (NOW + timedelta(minutes=6)).isoformat()
        result = self.process(b)
        self.assertIn("STALE_OR_FUTURE_SNAPSHOT", result["bundle_codes"])
        self.assertIn("MIXED_SNAPSHOTS", result["bundle_codes"])

    def test_account_identity_rejected(self):
        b = fixture()
        b["account"]["account_id"] = "wrong-account"
        with self.assertRaises(ValueError):
            self.process(b)

    def test_malformed_inputs_never_reserve(self):
        cases = []
        b = fixture(); b["cycle"]["records"][0]["quantity"] = 1.5; cases.append(b)
        b = fixture(); b["cycle"]["records"][0]["quantity"] = 2**63; cases.append(b)
        b = fixture(); b["cycle"]["records"][0]["proposal_hash"] = "wrong"; cases.append(b)
        b = fixture(); b["account"]["as_of"] = "2026-11-20T16:04:00"; cases.append(b)
        b = fixture(); del b["account"]["working_orders"]; cases.append(b)
        b = fixture(); b["account"]["cash"] = True; cases.append(b)
        for index, b in enumerate(cases):
            with self.subTest(index=index), self.assertRaises((ValueError, KeyError)):
                self.process(b)
        self.assertEqual(self.store.status()["reservations"], 0)

    def test_untrusted_bundle_cannot_enable_trading_or_supply_approval(self):
        b = fixture()
        b["trading_enabled"] = True
        with self.assertRaises(ValueError):
            self.process(b)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE control SET trading_enabled=1")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE control SET guard_halted=0")

    def test_nan_oversize_duplicate_json_keys_rejected(self):
        for data in (b'{"a":NaN}', b'{"a":1,"a":2}', b"x" * 2_000_001):
            with self.assertRaises(ValueError):
                decode(data)

    def test_snapshot_bundle_drops_notes_and_baseline(self):
        b = fixture()
        self.assertNotIn("note", b["account"])
        self.assertNotIn("day_open_equity", b["account"])

    def test_bundle_cli_preserves_timestamps_and_never_overwrites(self):
        bundle = fixture()
        args = []
        for name in ("cycle", "account", "metrics"):
            path = Path(self.tmp.name) / (name + ".json")
            path.write_bytes(payload(bundle[name]))
            args.extend(["--" + name, str(path)])
        output = Path(self.tmp.name) / "coordinator-20261120-1100.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bundle_main([*args, "--out", str(output)]), 0)
        self.assertEqual(decode(output.read_bytes()), bundle)
        original = output.read_bytes()
        with self.assertRaises(FileExistsError):
            bundle_main([*args, "--out", str(output)])
        self.assertEqual(output.read_bytes(), original)
        rerun = output.with_name("coordinator-20261120-1100-rerun.json")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bundle_main([*args, "--out", str(rerun)]), 0)
        self.assertEqual(rerun.read_bytes(), original)

    def test_bundle_cli_rejects_wrong_date_or_slot_without_writing(self):
        bundle = fixture()
        args = []
        for name in ("cycle", "account", "metrics"):
            path = Path(self.tmp.name) / (name + ".json")
            path.write_bytes(payload(bundle[name]))
            args.extend(["--" + name, str(path)])
        for name in ("coordinator-20261121-1100.json", "coordinator-20261120-0945.json"):
            output = Path(self.tmp.name) / name
            with self.subTest(name=name), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                bundle_main([*args, "--out", str(output)])
            self.assertFalse(output.exists())

    def test_bundle_cli_uses_eastern_date_not_utc_date(self):
        bundle = fixture(now=NOW.replace(day=21, hour=0, minute=1))
        bundle["cycle"]["records"] = []
        args = []
        for name in ("cycle", "account", "metrics"):
            path = Path(self.tmp.name) / (name + ".json")
            path.write_bytes(payload(bundle[name]))
            args.extend(["--" + name, str(path)])
        output = Path(self.tmp.name) / "coordinator-20261120-1100.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bundle_main([*args, "--out", str(output)]), 0)
        self.assertEqual(decode(output.read_bytes()), bundle)

    def test_r10_is_never_bypassed(self):
        b = fixture()
        b["cycle"]["records"][0]["evidence"][1]["as_of"] = (NOW - timedelta(hours=25)).isoformat()
        self.assertIn("R10_EVIDENCE_STALE", self.process(b)["decisions"][0]["codes"])

    def test_r8_uses_pinned_baseline(self):
        b = fixture()
        b["account"]["equity"] = 97000
        self.assertIn("R8_DAILY_STOP", self.process(b)["decisions"][0]["codes"])

    def test_cash_reserves_include_fees(self):
        b = fixture(("US.AMZN", "US.NVDA"), (3500, 3500))
        b["account"]["cash"] = 7000
        self.assertIn("R4_FUNDS", self.process(b)["decisions"][1]["codes"])

    def test_external_broker_activity_without_notionals_fails_closed(self):
        b = fixture()
        b["account"]["executed_orders_today"] = 1
        self.assertIn("BROKER_BUY_USAGE_UNVERIFIED", self.process(b)["bundle_codes"])

    def test_universe_sleeve_and_core_etf(self):
        b = fixture(("US.SPY",), (9900,))
        b["cycle"]["records"][0]["sleeve"] = "core"
        b["metrics"]["symbols"]["US.SPY"]["instrument_type"] = "etf"
        self.assertEqual(self.process(b)["decisions"][0]["status"], "DRY_RUN_RESERVED")
        b = fixture()
        b["cycle"]["records"][0]["sleeve"] = "core"
        self.assertIn("OUTSIDE_PILOT_UNIVERSE", self.process(b, fid="file_b")["decisions"][0]["codes"])

    def test_no_entries_at_exit_slot_or_closed_market(self):
        b = fixture(slot="15:30")
        self.assertIn("ENTRY_SESSION_CLOSED", self.process(b)["bundle_codes"])
        self.assertIn("ENTRY_SESSION_CLOSED", self.process(fid="file_b", now=NOW.replace(hour=22))["bundle_codes"])

    def test_utc_midnight_does_not_change_et_baseline_day(self):
        midnight = datetime(2026, 11, 21, 0, 1, tzinfo=timezone.utc)
        result = self.process(now=midnight)
        self.assertNotIn("BASELINE_MISSING", result["bundle_codes"])
        self.assertIn("ENTRY_SESSION_CLOSED", result["bundle_codes"])

    def test_carried_pending_reservations_do_not_disappear_next_et_day(self):
        self.process(fixture(("US.AMZN", "US.NVDA"), (3500, 3500)))
        day2 = NOW + timedelta(days=1) - timedelta(minutes=20)
        self.store.add_baseline("2026-11-21", 100000, day2.replace(hour=14, minute=30), "fixture day two")
        self.assertEqual(Store(self.db).status()["reservations"], 2)
        self.assertEqual(Store(self.db).status()["pending_notional"], 7000)

    def test_rejected_ticket_identity_is_pinned_too(self):
        self.process(fixture(costs=(11000,)))
        result = self.process(fixture(costs=(3500,)), fid="file_b")
        self.assertIn("TICKET_IDENTITY_COLLISION", result["decisions"][0]["codes"])
        self.assertEqual(self.store.status()["reservations"], 0)

    def test_revoked_duplicate_is_not_reported_as_new_eligibility(self):
        self.process()
        self.store.revoke(NOW)
        result = self.process(fid="file_b")
        self.assertEqual(result["decisions"][0]["status"], "DRY_RUN_REJECTED")
        self.assertIn("PILOT_REVOKED", result["decisions"][0]["codes"])

    def test_minimal_container_sources_boot_without_guard_or_broker(self):
        root = Path(__file__).resolve().parent.parent
        app = Path(self.tmp.name) / "container-app"
        app.mkdir()
        for package in ("coordinator", "gate", "scout"):
            shutil.copytree(root / package, app / package, ignore=shutil.ignore_patterns("__pycache__"))
        (app / "dashboard").mkdir()
        for name in ("__init__.py", "cloud.py"):
            shutil.copyfile(root / "dashboard" / name, app / "dashboard" / name)
        shutil.copyfile(root / "config.json", app / "config.json")
        self.assertFalse((app / "guard").exists())
        self.assertFalse((app / "adapters").exists())
        result = subprocess.run([sys.executable, "-m", "coordinator", "--help"], cwd=app,
                                env={**os.environ, "PYTHONPATH": ""}, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("permanently disabled", result.stdout)

    def test_log_file_records_revoked_ledger_without_network_or_renewal(self):
        self.store.revoke(NOW)
        original = self.store.status()
        env_file = Path(self.tmp.name) / "fixture.env"
        env_file.write_text("QODER_PAT_TKN=fixture-token-never-send\n", encoding="utf-8")
        root = Path(__file__).resolve().parent.parent
        args = [sys.executable, "-m", "coordinator", "--db", str(self.db), "run",
                "--env-file", str(env_file), "--once", "--log-file", str(Path(self.tmp.name) / "worker.log")]
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (Path(self.tmp.name) / "worker.log").read_text(encoding="utf-8-sig")
        status = json.loads(log)
        self.assertTrue(status["inactive"])
        self.assertTrue(status["guard_halted"])
        self.assertFalse(status["trading_enabled"])
        self.assertNotIn("fixture-token", log)
        self.assertEqual(self.store.status(), original)

    def test_unwritable_log_prevents_worker_start(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "-m", "coordinator", "--db", str(self.db), "run",
                                 "--env-file", str(Path(self.tmp.name) / "absent.env"), "--once",
                                 "--log-file", str(Path(self.tmp.name) / "absent" / "worker.log")], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot open local worker log", result.stdout)

    def test_reopened_process_reads_same_ledger(self):
        self.process()
        result = subprocess.run([sys.executable, "-m", "coordinator", "--db", str(self.db), "status"],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["reservations"], 1)
        self.assertFalse(status["trading_enabled"])
        self.assertTrue(status["guard_halted"])

    def test_process_crash_rolls_back_whole_bundle(self):
        code = '''
import os, sys
from tests.test_coordinator import fixture, payload, NOW, SID
from coordinator.store import Store
store = Store(sys.argv[1])
Store._audit = staticmethod(lambda *args: os._exit(7))
store.process(payload(fixture()), session_id=SID, file_id="file_crash", now=NOW)
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.db)],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(Store(self.db).status()["reservations"], 0)
        self.assertEqual(self.process()["decisions"][0]["status"], "DRY_RUN_RESERVED")

    def test_next_et_day_pending_quota_and_new_baseline(self):
        first = NOW.replace(day=23)
        second = NOW.replace(day=24, hour=14, minute=46)
        db = Path(self.tmp.name) / "rollover.sqlite3"
        store = Store.initialize(db, pilot_id="rollover", starts_at=first - timedelta(minutes=10), scout_agent_id=AGENT)
        store.add_baseline("2026-11-23", 100000, first.replace(hour=14, minute=30), "fixture Monday")
        store.process(payload(fixture(("US.AMZN", "US.NVDA"), (3500, 3500), now=first)),
                      session_id=SID, file_id="file_first", now=first)
        store.add_baseline("2026-11-24", 100000, second.replace(minute=30), "fixture Tuesday")
        result = store.process(payload(fixture(("US.MSFT",), (3500,), now=second, slot="09:45")),
                               session_id=SID, file_id="file_second", now=second)
        self.assertIn("PILOT_BUY_COUNT", result["decisions"][0]["codes"])
        self.assertNotIn("PILOT_INACTIVE", result["bundle_codes"])

    def test_cli_has_no_enable_submit_or_resume(self):
        for command in ("enable", "submit", "resume", "grant"):
            with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                main(["--db", str(self.db), command])

    def test_cli_does_not_create_missing_database(self):
        with redirect_stdout(io.StringIO()):
            result = main(["--db", str(Path(self.tmp.name) / "missing.sqlite3"), "status"])
        self.assertEqual(result, 2)


class FakeCloud:
    def __init__(self, bundle=None):
        self.bundle = bundle or fixture()
        self.downloads = []
        self.session = {"id": SID, "agent": {"id": AGENT}, "updated_at": NOW.isoformat()}
        self.meta = {"id": "file_a", "filename": "coordinator-20261120-1100.json", "scope_id": SID}

    def iter_sessions(self, agent_id):
        return [self.session]

    def iter_files(self, sid):
        return [self.meta]

    def download_file(self, fid):
        self.downloads.append(fid)
        return payload(self.bundle)

    def send_user_message(self, *args, **kwargs):
        raise AssertionError("must never dispatch to Guard")


class IntakeTests(CoordinatorFixture):
    def test_cloud_only_poll_needs_no_dashboard_or_runtime_files(self):
        cloud = FakeCloud()
        result = poll_once(cloud, self.store, clock=lambda: NOW)
        self.assertEqual(result["bundles"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["decisions"][0]["status"], "DRY_RUN_RESERVED")
        self.assertEqual(poll_once(cloud, Store(self.db), clock=lambda: NOW)["bundles"], 0)
        self.assertEqual(cloud.downloads, ["file_a"])

    def test_missing_baseline_visible_even_when_no_bundle_arrives(self):
        store = Store.initialize(Path(self.tmp.name) / "no-baseline.sqlite3", pilot_id="no-baseline",
                                 starts_at=NOW - timedelta(minutes=10), scout_agent_id=AGENT)
        cloud = FakeCloud()
        cloud.iter_sessions = lambda _: []
        result = poll_once(cloud, store, clock=lambda: NOW)
        self.assertTrue(result["baseline_missing"])
        self.assertEqual(result["bundles"], 0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(store.status()["baseline_days"], [])
        self.assertTrue(result["guard_halted"])
        self.assertFalse(result["trading_enabled"])

    def test_wrong_agent_is_not_downloaded(self):
        cloud = FakeCloud()
        cloud.session["agent"]["id"] = "agent_other"
        self.assertEqual(poll_once(cloud, self.store, clock=lambda: NOW)["bundles"], 0)
        self.assertEqual(cloud.downloads, [])

    def test_cross_session_file_rejected(self):
        cloud = FakeCloud()
        cloud.meta["scope_id"] = "sess_other"
        self.assertTrue(poll_once(cloud, self.store, clock=lambda: NOW)["errors"])
        self.assertEqual(self.store.status()["reservations"], 0)

    def test_poll_rechecks_time_after_download(self):
        times = iter([NOW, NOW + timedelta(minutes=31), NOW + timedelta(minutes=31)])
        result = poll_once(FakeCloud(), self.store, clock=lambda: next(times))
        self.assertIn("STALE_OR_FUTURE_SNAPSHOT", result["decisions"][0]["codes"])

    def test_cloud_error_stops_without_logging_raw_content(self):
        cloud = FakeCloud()
        cloud.download_file = lambda _: (_ for _ in ()).throw(CloudError(500, "sensitive example"))
        result = poll_once(cloud, self.store, clock=lambda: NOW)
        self.assertTrue(result["errors"])
        self.assertNotIn("sensitive example", json.dumps(result))

    def test_readonly_client_refuses_post_before_transport(self):
        calls = []
        client = ReadOnlyCloud("test-only", transport=lambda *args: calls.append(args))
        with self.assertRaises(CloudError):
            client.send_user_message(SID, "not allowed")
        self.assertEqual(calls, [])

    def test_pagination_visits_second_page(self):
        calls = []
        def transport(request, timeout):
            calls.append(request.full_url)
            if "after_id=" in request.full_url:
                return 200, json.dumps({"data": [{"id": "sess_two"}], "has_more": False}).encode()
            return 200, json.dumps({"data": [{"id": SID}], "has_more": True, "last_id": SID}).encode()
        client = ReadOnlyCloud("test-only", transport=transport)
        self.assertEqual(len(list(client.iter_sessions(AGENT))), 2)
        self.assertIn("after_id=sess_scout", calls[1])

    def test_http_intake_pagination_download_restart_and_no_writes(self):
        requests = []
        body = payload(fixture())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.command, self.path, self.headers.get("Authorization")))
                path = urlsplit(self.path).path
                if path == "/sessions":
                    result = {"data": [{"id": SID, "agent": {"id": AGENT}, "updated_at": NOW.isoformat()}],
                              "has_more": False}
                elif path == "/files":
                    if "after_id=" in self.path:
                        result = {"data": [{"id": "file_bundle", "scope_id": SID,
                                            "filename": "coordinator-20261120-1100.json"}], "has_more": False}
                    else:
                        result = {"data": [{"id": "file_other", "filename": "metrics.json"}],
                                  "has_more": True, "last_id": "file_other"}
                elif path == "/files/file_bundle/content":
                    result = {"url": "https://artifact.test/bundle"}
                elif path == "/bundle":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def do_POST(self):
                requests.append((self.command, self.path, None))
                self.send_error(405)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            def transport(request, timeout):
                if request.full_url.startswith("https://artifact.test/"):
                    self.assertIsNone(request.get_header("Authorization"))
                    request = Request(base + "/bundle", headers=dict(request.headers), method=request.get_method())
                return readonly_transport(request, timeout)
            client = ReadOnlyCloud("fixture-token-not-real", base_url=base, transport=transport)
            first = poll_once(client, self.store, clock=lambda: NOW)
            self.assertEqual(first["errors"], [])
            self.assertEqual(first["decisions"][0]["status"], "DRY_RUN_RESERVED")
            second = poll_once(client, Store(self.db), clock=lambda: NOW)
            self.assertEqual(second["bundles"], 0)
            self.assertTrue(all(r[0] == "GET" for r in requests))
            self.assertEqual([r for r in requests if r[1] == "/bundle"], [("GET", "/bundle", None)])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_missing_or_stuck_pagination_fails_closed(self):
        for result in ({"data": []}, {"data": [], "has_more": True, "last_id": SID}):
            client = ReadOnlyCloud("test-only", transport=lambda *args: (200, json.dumps(result).encode()))
            with self.assertRaises(CloudError):
                list(client.iter_sessions(AGENT))


if __name__ == "__main__":
    unittest.main()
