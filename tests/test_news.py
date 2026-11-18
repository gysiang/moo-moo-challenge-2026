from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from scout import news

NOW = datetime(2026, 11, 18, 14, 50, 0, tzinfo=timezone.utc)


def item(ident: str, published, **extra):
    row = {"news_id": ident, "publish_time": published, "source": "富途资讯", "title": "t"}
    row.update(extra)
    return row


class ParseMomentTests(unittest.TestCase):
    def test_epoch_ms_and_seconds_agree(self):
        ms = news.parse_moment(1795011819000)
        seconds = news.parse_moment(1795011819)
        self.assertEqual(ms, seconds)
        self.assertEqual(ms.isoformat(), "2026-11-18T14:23:39+00:00")

    def test_iso_strings(self):
        self.assertEqual(
            news.parse_moment("2026-11-18T14:23:39Z").isoformat(),
            "2026-11-18T14:23:39+00:00",
        )
        self.assertEqual(
            news.parse_moment("2026-11-18T09:23:39-05:00").isoformat(),
            "2026-11-18T14:23:39+00:00",
        )

    def test_rejects_junk(self):
        for value in (None, "", "soon", True, 0, -5, [], {}):
            self.assertIsNone(news.parse_moment(value), value)


class NormalizeTests(unittest.TestCase):
    def test_stale_future_and_timestampless_items_are_dropped(self):
        raw = {
            "US.LLY": {
                "ret_code": 0,
                "data": {
                    "items": [
                        item("1", 1794922260000),
                        item("2", 1795000000000),
                        item("3", "not-a-date"),
                        {"news_id": "4", "title": "no timestamp"},
                        item("5", int((NOW + timedelta(minutes=10)).timestamp() * 1000)),
                        item("6", int((NOW + timedelta(minutes=2)).timestamp() * 1000)),
                    ]
                },
            }
        }
        evidence, report = news.normalize(raw, now=NOW)
        ids = [row["id"] for row in evidence["US.LLY"]]
        self.assertEqual(ids, ["N-6", "N-2"])
        self.assertEqual(report["US.LLY"], {"rows": 6, "kept": 2, "dropped": 4})

    def test_newest_first_and_limited(self):
        items = [item(str(i), 1795000000000 + i * 60_000) for i in range(5)]
        evidence, _ = news.normalize({"US.PG": items}, now=NOW)
        self.assertEqual([row["id"] for row in evidence["US.PG"]], ["N-4", "N-3", "N-2"])

    def test_accepts_bare_list_and_data_list(self):
        bare, _ = news.normalize({"US.AAPL": [item("11", 1795000000000)]}, now=NOW)
        wrapped, _ = news.normalize(
            {"US.AAPL": {"data": [item("11", 1795000000000)]}}, now=NOW
        )
        self.assertEqual(bare, wrapped)
        self.assertEqual(bare["US.AAPL"][0]["as_of"], "2026-11-18T11:06:40Z")

    def test_source_default_and_title_clip(self):
        raw = {"US.WMT": [item("9", 1795000000000, source="", title="x" * 400)]}
        evidence, _ = news.normalize(raw, now=NOW)
        row = evidence["US.WMT"][0]
        self.assertEqual(row["source"], "moomoo.news_search")
        self.assertLessEqual(len(row["title"]), news.TITLE_LIMIT)

    def test_non_object_input_is_rejected(self):
        with self.assertRaises(ValueError):
            news.normalize([], now=NOW)


class CliTests(unittest.TestCase):
    def test_writes_evidence_file(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "mcp-news.json"
            out = Path(tmp) / "news.json"
            source.write_text(
                json.dumps(
                    {
                        "US.AAPL": {
                            "data": {
                                "items": [
                                    item("1", 1795011819000),
                                    item("2", 1794922260000),
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            code = news.main(
                ["--in", str(source), "--out", str(out), "--now", "2026-11-18T14:50:00Z"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in payload["US.AAPL"]], ["N-1"])
        self.assertEqual(payload["US.AAPL"][0]["as_of"], "2026-11-18T14:23:39Z")

    def test_rejects_bad_limit(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "mcp-news.json"
            source.write_text("{}", encoding="utf-8")
            code = news.main(
                ["--in", str(source), "--out", str(Path(tmp) / "n.json"), "--limit", "0"]
            )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
