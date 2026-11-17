from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scout.ingest import main, normalise


def klines(count: int, field: str = "close", start: float = 100.0) -> list[dict]:
    return [
        {"time_key": f"2026-09-{index + 1:02d}", field: start + index, "high": start + index + 0.5}
        for index in range(count)
    ]


class TestIngest(unittest.TestCase):
    def test_klines_are_mapped_to_closes_and_highs(self) -> None:
        payload = {
            "as_of": "2026-11-16T21:00:00Z",
            "symbols": {
                "US.AAPL": {"market_cap": 3.5e12, "klines": klines(60)},
            },
        }
        raw = normalise(payload)
        row = raw["symbols"]["US.AAPL"]
        self.assertEqual(len(row["closes"]), 60)
        self.assertEqual(len(row["highs"]), 60)
        self.assertEqual(row["instrument_type"], "stock")
        self.assertEqual(raw["as_of"], "2026-11-16T21:00:00Z")

    def test_price_field_aliases_are_accepted(self) -> None:
        payload = {
            "as_of": "2026-11-16T21:00:00Z",
            "symbols": {"US.SPY": {"market_cap": 6e11, "klines": klines(55, "close_price")}},
        }
        self.assertEqual(normalise(payload)["symbols"]["US.SPY"]["closes"][0], 100.0)

    def test_klines_are_sorted_by_time_key(self) -> None:
        rows = klines(51)
        payload = {
            "as_of": "2026-11-16T21:00:00Z",
            "symbols": {"US.QQQ": {"market_cap": 2e11, "klines": list(reversed(rows))}},
        }
        closes = normalise(payload)["symbols"]["US.QQQ"]["closes"]
        self.assertEqual(closes, sorted(closes))

    def test_already_raw_rows_pass_through(self) -> None:
        payload = {
            "as_of": "2026-11-16T21:00:00Z",
            "symbols": {
                "US.IWM": {
                    "instrument_type": "etf",
                    "market_cap": 7e10,
                    "closes": [float(i) for i in range(50)],
                    "highs": [float(i) + 1 for i in range(20)],
                }
            },
        }
        row = normalise(payload)["symbols"]["US.IWM"]
        self.assertEqual(len(row["closes"]), 50)
        self.assertEqual(row["instrument_type"], "etf")

    def test_too_few_bars_is_an_error(self) -> None:
        payload = {
            "as_of": "2026-11-16T21:00:00Z",
            "symbols": {"US.TSLA": {"market_cap": 1e12, "klines": klines(49)}},
        }
        with self.assertRaises(ValueError):
            normalise(payload)

    def test_missing_price_field_names_the_symbol(self) -> None:
        payload = {
            "as_of": "2026-11-16T21:00:00Z",
            "symbols": {
                "US.META": {"market_cap": 1e12, "klines": [{"t": "x", "px": 1.0}] * 60}
            },
        }
        with self.assertRaisesRegex(ValueError, "US.META"):
            normalise(payload)

    def test_cli_writes_raw_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "mcp.json"
            out = Path(temp) / "market-raw.json"
            source.write_text(
                json.dumps(
                    {
                        "as_of": "2026-11-16T21:00:00Z",
                        "symbols": {"US.AAPL": {"market_cap": 3.5e12, "klines": klines(60)}},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(main(["--in", str(source), "--out", str(out)]), 0)
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("US.AAPL", written["symbols"])


if __name__ == "__main__":
    unittest.main()
