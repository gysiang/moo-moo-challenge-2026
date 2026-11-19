from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from dashboard.cloud import CloudError
from dashboard.sync import sync_once

NOW = datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc)


class FakeCloud:
    def __init__(self, sessions=None, files=None, contents=None, failing=None):
        self._sessions = sessions or []
        self._files = files or {}
        self._contents = contents or {}
        self._failing = set(failing or ())
        self.downloads = []

    def sessions(self, *, limit=12):
        return self._sessions

    def files(self, *, scope_id=None, limit=20):
        return self._files.get(scope_id, [])

    def download_file(self, file_id):
        self.downloads.append(file_id)
        if file_id in self._failing:
            raise CloudError(500, "storage exploded")
        return self._contents[file_id]


def session(sid="sess_a", agent="moomoo-scout", updated="2026-11-18T16:00:00Z"):
    return {"id": sid, "agent": {"name": agent}, "updated_at": updated}


def meta(file_id, name):
    return {"id": file_id, "filename": name}


def cycle_payload(slot="09:45"):
    return json.dumps(
        {"slot": slot, "generated_at": NOW.isoformat(), "trading_day": True, "records": []}
    ).encode("utf-8")


class SyncTests(unittest.TestCase):
    def run_sync(self, rt, cloud):
        return sync_once(cloud, Path(rt), now=NOW)

    def test_cycle_written_once_under_dated_name(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "cycle-0945.json")]},
            contents={"file_1": cycle_payload()},
        )
        with TemporaryDirectory() as rt:
            first = self.run_sync(rt, cloud)
            self.assertEqual(len(first["written"]), 1)
            self.assertEqual(first["written"][0]["file"], "cycle-20261118-0945.json")
            self.assertTrue((Path(rt) / "cycle-20261118-0945.json").exists())
            self.assertFalse((Path(rt) / "cycle-0945.json").exists())
            second = self.run_sync(rt, cloud)
            self.assertEqual(second["written"], [])
            self.assertEqual(second["skipped"][0]["reason"], "already present")

    def test_same_slot_on_two_days_does_not_collide(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "cycle-0945.json")]},
            contents={"file_1": cycle_payload()},
        )
        with TemporaryDirectory() as rt:
            self.assertEqual(len(self.run_sync(rt, cloud)["written"]), 1)
            next_day = cycle_payload().replace(b"2026-11-18", b"2026-11-19")
            cloud._contents["file_1"] = next_day
            self.assertEqual(len(self.run_sync(rt, cloud)["written"]), 1)
            names = sorted(p.name for p in Path(rt).glob("cycle-*.json"))
            self.assertEqual(names, ["cycle-20261118-0945.json", "cycle-20261119-0945.json"])

    def test_snapshot_rewritten_only_when_changed(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "account-5829804.json")]},
            contents={"file_1": b'{"account_id": "5829804", "equity": 1}'},
        )
        with TemporaryDirectory() as rt:
            self.assertEqual(len(self.run_sync(rt, cloud)["written"]), 1)
            same = self.run_sync(rt, cloud)
            self.assertEqual(same["written"], [])
            self.assertEqual(same["skipped"][0]["reason"], "unchanged")
            cloud._contents["file_1"] = b'{"account_id": "5829804", "equity": 2}'
            self.assertEqual(len(self.run_sync(rt, cloud)["written"]), 1)
            self.assertIn(b'"equity": 2', (Path(rt) / "account-5829804.json").read_bytes())

    def test_guard_events_append_dedupes(self):
        with TemporaryDirectory() as rt:
            path = Path(rt) / "guard-events.jsonl"
            path.write_text('{"event": "ORDER_SUBMITTED"}\n', encoding="utf-8")
            cloud = FakeCloud(
                sessions=[session("sess_g", agent="moomoo-guard")],
                files={"sess_g": [meta("file_1", "guard-events.jsonl")]},
                contents={
                    "file_1": b'{"event": "ORDER_SUBMITTED"}\n{"event": "RECONCILED"}\n'
                },
            )
            summary = self.run_sync(rt, cloud)
            self.assertEqual(summary["written"][0]["action"], "append")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            again = self.run_sync(rt, cloud)
            self.assertEqual(again["written"], [])
            self.assertEqual(again["skipped"][0]["reason"], "no new rows")

    def test_unknown_and_nested_names_skipped(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={
                "sess_a": [
                    meta("file_1", "notes.txt"),
                    meta("file_2", "sub/cycle-0945.json"),
                ]
            },
            contents={"file_1": b"hello", "file_2": cycle_payload()},
        )
        with TemporaryDirectory() as rt:
            summary = self.run_sync(rt, cloud)
            self.assertEqual(summary["written"], [])
            self.assertEqual(summary["files"], 0)
            self.assertEqual(cloud.downloads, [])

    def test_invalid_cycle_payload_rejected(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "cycle-0945.json")]},
            contents={"file_1": b"{}"},
        )
        with TemporaryDirectory() as rt:
            summary = self.run_sync(rt, cloud)
            self.assertEqual(summary["written"], [])
            self.assertEqual(summary["skipped"][0]["reason"], "cycle payload has no slot/records")
            self.assertEqual(list(Path(rt).glob("cycle-*.json")), [])

    def test_cycle_without_generated_at_rejected(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "cycle-0945.json")]},
            contents={"file_1": b'{"slot": "09:45", "records": []}'},
        )
        with TemporaryDirectory() as rt:
            summary = self.run_sync(rt, cloud)
            self.assertEqual(summary["written"], [])
            self.assertEqual(summary["skipped"][0]["reason"], "cycle payload has no usable generated_at/slot")

    def test_oversize_payload_rejected(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "guard-events.jsonl")]},
            contents={"file_1": b"x" * 2_000_001},
        )
        with TemporaryDirectory() as rt:
            summary = self.run_sync(rt, cloud)
            self.assertEqual(summary["written"], [])
            self.assertIn("exceeds", summary["skipped"][0]["reason"])

    def test_session_filter_by_agent_and_age(self):
        cloud = FakeCloud(
            sessions=[
                session("sess_a", updated="2026-11-18T16:00:00Z"),
                session("sess_b", agent="someone-else"),
                session("sess_c", updated="2026-11-16T00:00:00Z"),
            ],
            files={"sess_a": [], "sess_b": [], "sess_c": []},
            contents={},
        )
        with TemporaryDirectory() as rt:
            summary = self.run_sync(rt, cloud)
            self.assertEqual(summary["sessions"], 1)

    def test_download_error_recorded_not_fatal(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "cycle-0945.json"), meta("file_2", "metrics.json")]},
            contents={"file_1": cycle_payload(), "file_2": b'{"symbols": {}}'},
            failing={"file_1"},
        )
        with TemporaryDirectory() as rt:
            summary = self.run_sync(rt, cloud)
            self.assertEqual(summary["errors"][0]["file"], "cycle-0945.json")
            self.assertEqual([row["file"] for row in summary["written"]], ["metrics.json"])

    def test_sync_log_written(self):
        cloud = FakeCloud(
            sessions=[session()],
            files={"sess_a": [meta("file_1", "metrics.json")]},
            contents={"file_1": b'{"symbols": {}}'},
        )
        with TemporaryDirectory() as rt:
            self.run_sync(rt, cloud)
            rows = (Path(rt) / "sync.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["file"], "metrics.json")


if __name__ == "__main__":
    unittest.main()
