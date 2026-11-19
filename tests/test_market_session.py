from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dashboard.state import build_state
from scout.slots import market_session

CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class MarketSessionTests(unittest.TestCase):
    def test_open_mid_session(self) -> None:
        m = market_session(at(2026, 11, 19, 15, 0))  # 10:00 ET Thursday
        self.assertEqual(m["state"], "OPEN")
        self.assertEqual(m["reason"], "regular session")
        self.assertEqual(m["session_open"], "09:30")
        self.assertEqual(m["session_close"], "16:00")
        self.assertEqual(m["closes_at"], "2026-11-19T16:00:00-05:00")
        self.assertIsNone(m["next_open"])

    def test_open_at_the_bell(self) -> None:
        self.assertEqual(market_session(at(2026, 11, 19, 14, 30))["state"], "OPEN")

    def test_closed_one_minute_before_the_bell(self) -> None:
        m = market_session(at(2026, 11, 19, 14, 29))
        self.assertEqual(m["state"], "CLOSED")
        self.assertEqual(m["reason"], "before the open")
        self.assertEqual(m["next_open"], "2026-11-19T09:30:00-05:00")

    def test_closed_at_the_bell(self) -> None:
        m = market_session(at(2026, 11, 19, 21, 0))  # 16:00 ET
        self.assertEqual(m["state"], "CLOSED")
        self.assertEqual(m["reason"], "after the close")
        self.assertEqual(m["next_open"], "2026-11-20T09:30:00-05:00")

    def test_weekend_points_at_monday(self) -> None:
        m = market_session(at(2026, 11, 21, 15, 0))  # Saturday
        self.assertEqual(m["state"], "CLOSED")
        self.assertEqual(m["reason"], "weekend")
        self.assertFalse(m["trading_day"])
        self.assertEqual(m["next_open"], "2026-11-23T09:30:00-05:00")

    def test_holiday_points_at_the_next_session(self) -> None:
        m = market_session(at(2026, 11, 26, 15, 0))  # Thanksgiving
        self.assertEqual(m["reason"], "holiday")
        self.assertEqual(m["next_open"], "2026-11-27T09:30:00-05:00")

    def test_early_close_day_is_open_until_1300(self) -> None:
        m = market_session(at(2026, 11, 27, 17, 59))  # 12:59 ET
        self.assertEqual(m["state"], "OPEN")
        self.assertEqual(m["reason"], "early close")
        self.assertTrue(m["early_close"])
        self.assertEqual(m["session_close"], "13:00")
        self.assertEqual(m["closes_at"], "2026-11-27T13:00:00-05:00")

    def test_early_close_day_closes_at_1300(self) -> None:
        m = market_session(at(2026, 11, 27, 18, 0))  # 13:00 ET
        self.assertEqual(m["state"], "CLOSED")
        self.assertEqual(m["next_open"], "2026-11-30T09:30:00-05:00")

    def test_summer_session_uses_edt(self) -> None:
        m = market_session(at(2026, 7, 15, 14, 0))  # 10:00 EDT
        self.assertEqual(m["state"], "OPEN")
        self.assertEqual(m["closes_at"], "2026-07-15T16:00:00-04:00")

    def test_naive_datetime_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            market_session(datetime(2026, 11, 19, 15, 0))


class DashboardMarketTests(unittest.TestCase):
    def test_state_carries_the_session_for_its_own_timestamp(self) -> None:
        moment = at(2026, 11, 19, 15, 0)
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_state(
                runtime=Path(tmp), config_path=CONFIG, baseline=100000.0, now=moment
            )
        self.assertEqual(payload["market"], market_session(moment))
        self.assertEqual(payload["market"]["state"], "OPEN")
        self.assertEqual(payload["as_of"], moment.isoformat())

    def test_state_reports_closed_outside_the_session(self) -> None:
        moment = at(2026, 11, 21, 15, 0)
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_state(
                runtime=Path(tmp), config_path=CONFIG, baseline=100000.0, now=moment
            )
        self.assertEqual(payload["market"]["state"], "CLOSED")
        self.assertEqual(payload["market"]["reason"], "weekend")


if __name__ == "__main__":
    unittest.main()