from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from gate import Approval, Evidence, Position, load_limits
from gate.models import AccountState
from scout.account import load_account, load_approvals, save_approval
from scout.cycle import Scout
from scout.marketdata import build_snapshot, metrics_from_history
from scout.models import AWAITING_APPROVAL, READY, REJECTED, Metrics, Snapshot
from scout.settings import StrategySettings, load_strategy
from scout.slots import (
    HOLIDAYS,
    allows_entries,
    due_slot,
    eastern,
    is_trading_day,
    session_close,
    slot_available,
)
from scout.strategy import build_candidates
from scout.watchlist import CoreHolding, Watchlist

NOW = datetime(2026, 11, 17, 14, 45, tzinfo=timezone.utc)  # 09:45 ET
SNAPSHOT_AS_OF = datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)
CONFIG = Path(__file__).resolve().parent.parent / "config.json"
LIMITS = load_limits(CONFIG)
SETTINGS = load_strategy(CONFIG)
WATCHLIST = Watchlist(
    core=(
        CoreHolding("US.SPY", 0.30),
        CoreHolding("US.QQQ", 0.20),
        CoreHolding("US.IWM", 0.10),
    ),
    satellites=("US.AAPL", "US.MSFT", "US.NVDA"),
)


def make_metrics(
    symbol: str,
    last: float,
    sma20: float,
    sma50: float,
    high20: float,
    roc20: float = 0.05,
    instrument_type: str = "stock",
    market_cap: float = 1e12,
) -> Metrics:
    return Metrics(
        symbol=symbol,
        instrument_type=instrument_type,
        last_price=last,
        market_cap=market_cap,
        sma20=sma20,
        sma50=sma50,
        high20=high20,
        roc20=roc20,
        as_of=SNAPSHOT_AS_OF,
    )


def make_snapshot(**overrides: Metrics) -> Snapshot:
    base = {
        "US.SPY": make_metrics("US.SPY", 100.0, 98.0, 95.0, 101.0, instrument_type="etf"),
        "US.QQQ": make_metrics("US.QQQ", 200.0, 195.0, 190.0, 202.0, instrument_type="etf"),
        "US.IWM": make_metrics("US.IWM", 50.0, 49.0, 48.0, 51.0, instrument_type="etf"),
        "US.AAPL": make_metrics("US.AAPL", 100.0, 98.0, 95.0, 101.0, roc20=0.08),
        "US.MSFT": make_metrics("US.MSFT", 100.0, 99.0, 101.0, 102.0, roc20=0.02),
        "US.NVDA": make_metrics("US.NVDA", 100.0, 97.0, 94.0, 110.0, roc20=0.12),
    }
    base.update(overrides)
    return Snapshot(as_of=SNAPSHOT_AS_OF, metrics=base)


def make_account(
    cash: float = 100_000.0,
    equity: float = 100_000.0,
    positions: dict[str, Position] | None = None,
    executed: int = 0,
) -> AccountState:
    return AccountState(
        now=NOW,
        cash=cash,
        equity=equity,
        positions=positions or {},
        executed_orders_today=executed,
    )


def fresh_news(symbol: str) -> tuple[Evidence, ...]:
    return (
        Evidence(
            evidence_id=f"N-{symbol}",
            kind="news",
            as_of=NOW - timedelta(hours=2),
            source="moomoo.news_search",
        ),
    )


def make_scout() -> Scout:
    return Scout(LIMITS, SETTINGS, WATCHLIST)


class TestSlots(unittest.TestCase):
    def test_eastern_offset_november_is_est(self) -> None:
        local = eastern(NOW)
        self.assertEqual(local.utcoffset(), timedelta(hours=-5))
        self.assertEqual((local.hour, local.minute), (9, 45))

    def test_eastern_offset_july_is_edt(self) -> None:
        summer = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(eastern(summer).utcoffset(), timedelta(hours=-4))

    def test_trading_days(self) -> None:
        self.assertTrue(is_trading_day(date(2026, 11, 17)))
        self.assertFalse(is_trading_day(date(2026, 11, 21)))  # Saturday
        self.assertFalse(is_trading_day(date(2026, 11, 26)))  # Thanksgiving
        self.assertIn(date(2026, 11, 26), HOLIDAYS)

    def test_slot_availability_and_early_close(self) -> None:
        self.assertTrue(slot_available("15:30", date(2026, 11, 17)))
        self.assertFalse(slot_available("15:30", date(2026, 11, 27)))
        self.assertTrue(slot_available("11:00", date(2026, 11, 27)))
        self.assertEqual(session_close(date(2026, 11, 27)), time(13, 0))

    def test_entries_blocked_in_last_30_minutes(self) -> None:
        day = date(2026, 11, 17)
        self.assertTrue(allows_entries("09:45", day))
        self.assertTrue(allows_entries("13:30", day))
        self.assertFalse(allows_entries("15:30", day))

    def test_due_slot(self) -> None:
        self.assertEqual(due_slot(datetime(2026, 11, 17, 14, 47, tzinfo=timezone.utc)), "09:45")
        self.assertEqual(due_slot(datetime(2026, 11, 17, 16, 5, tzinfo=timezone.utc)), "11:00")
        self.assertIsNone(due_slot(datetime(2026, 11, 17, 15, 10, tzinfo=timezone.utc)))
        self.assertIsNone(due_slot(datetime(2026, 11, 21, 14, 47, tzinfo=timezone.utc)))
        self.assertIsNone(due_slot(datetime(2026, 11, 26, 14, 47, tzinfo=timezone.utc)))


class TestMarketData(unittest.TestCase):
    def test_metrics_from_history(self) -> None:
        closes = [float(i + 1) for i in range(60)]  # 1..60
        highs = [float(i + 2) for i in range(60)]
        metrics = metrics_from_history("US.TST", "stock", 1e9, closes, highs, SNAPSHOT_AS_OF)
        self.assertAlmostEqual(metrics.sma20, sum(range(41, 61)) / 20)
        self.assertAlmostEqual(metrics.sma50, sum(range(11, 61)) / 50)
        self.assertEqual(metrics.high20, 61.0)
        self.assertAlmostEqual(metrics.roc20, 60 / 40 - 1)
        self.assertTrue(metrics.trend_up)

    def test_requires_50_bars(self) -> None:
        with self.assertRaises(ValueError):
            metrics_from_history("US.TST", "stock", 1e9, [1.0] * 49, [1.0] * 20, SNAPSHOT_AS_OF)

    def test_build_snapshot_from_raw(self) -> None:
        closes = [100.0 + i for i in range(60)]
        raw = {
            "as_of": "2026-11-16T21:00:00Z",
            "symbols": {
                "US.SPY": {
                    "instrument_type": "etf",
                    "market_cap": 6e11,
                    "closes": closes,
                    "highs": [c + 1 for c in closes],
                }
            },
        }
        snapshot = build_snapshot(raw)
        self.assertEqual(snapshot.metrics["US.SPY"].instrument_type, "etf")
        self.assertEqual(snapshot.as_of, SNAPSHOT_AS_OF)


class TestStrategy(unittest.TestCase):
    def test_core_entry_sizing(self) -> None:
        candidates, skipped = build_candidates(
            make_snapshot(), {}, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        core = [c for c in candidates if c.sleeve == "core"]
        spy = next(c for c in core if c.symbol == "US.SPY")
        self.assertEqual(spy.quantity, 100)  # 10,000 tranche / 100.00
        self.assertEqual(spy.reason, "CORE_ENTRY")

    def test_satellite_entry_requires_near_high(self) -> None:
        candidates, skipped = build_candidates(
            make_snapshot(), {}, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        satellites = [c.symbol for c in candidates if c.sleeve == "satellite"]
        self.assertEqual(satellites, ["US.AAPL"])  # NVDA is 9% below its 20-day high
        reasons = {skip.symbol: skip.reason for skip in skipped}
        self.assertEqual(reasons["US.MSFT"], "TREND_DOWN")
        self.assertEqual(reasons["US.NVDA"], "NOT_NEAR_HIGH")

    def test_ranking_by_momentum(self) -> None:
        watchlist = replace(WATCHLIST, satellites=("US.AAPL", "US.NVDA"))
        snapshot = make_snapshot(
            **{
                "US.AAPL": make_metrics("US.AAPL", 100.0, 98.0, 95.0, 101.0, roc20=0.02),
                "US.NVDA": make_metrics("US.NVDA", 100.0, 97.0, 94.0, 101.0, roc20=0.12),
            }
        )
        candidates, _ = build_candidates(
            snapshot, {}, 100_000.0, SETTINGS, watchlist, allow_entries=True
        )
        satellites = [c.symbol for c in candidates if c.sleeve == "satellite"]
        self.assertEqual(satellites, ["US.NVDA", "US.AAPL"])

    def test_satellite_slots_capped(self) -> None:
        symbols = tuple(f"US.S{index}" for index in range(8))
        snapshot = make_snapshot(
            **{
                symbol: make_metrics(symbol, 100.0, 98.0, 95.0, 101.0)
                for symbol in symbols
            }
        )
        watchlist = replace(WATCHLIST, satellites=symbols)
        candidates, skipped = build_candidates(
            snapshot, {}, 100_000.0, SETTINGS, watchlist, allow_entries=True
        )
        satellites = [c for c in candidates if c.sleeve == "satellite"]
        self.assertEqual(len(satellites), 6)
        self.assertEqual([s.reason for s in skipped].count("NO_SLOT"), 2)

    def test_entries_blocked_after_1530(self) -> None:
        candidates, skipped = build_candidates(
            make_snapshot(), {}, 100_000.0, SETTINGS, WATCHLIST, allow_entries=False
        )
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0].reason, "ENTRIES_BLOCKED_LAST_30_MIN")

    def test_exit_below_sma50(self) -> None:
        positions = {
            "US.AAPL": Position("US.AAPL", 10, 1000.0, 100.0),
        }
        snapshot = make_snapshot(
            **{"US.AAPL": make_metrics("US.AAPL", 90.0, 95.0, 95.0, 110.0)}
        )
        candidates, _ = build_candidates(
            snapshot, positions, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        exit_order = next(c for c in candidates if c.sleeve == "exit")
        self.assertEqual(exit_order.side, "SELL")
        self.assertEqual(exit_order.quantity, 10)
        self.assertEqual(exit_order.reason, "EXIT_BELOW_SMA50")

    def test_stop_loss_exit(self) -> None:
        positions = {"US.AAPL": Position("US.AAPL", 10, 900.0, 100.0)}
        snapshot = make_snapshot(
            **{"US.AAPL": make_metrics("US.AAPL", 90.0, 92.0, 85.0, 110.0)}
        )
        candidates, _ = build_candidates(
            snapshot, positions, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        exit_order = next(c for c in candidates if c.sleeve == "exit")
        self.assertEqual(exit_order.reason, "EXIT_STOP_LOSS")

    def test_core_exits_only_on_regime_break(self) -> None:
        positions = {"US.SPY": Position("US.SPY", 100, 9000.0, 150.0)}
        candidates, _ = build_candidates(
            make_snapshot(), positions, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        self.assertFalse(any(c.sleeve == "exit" for c in candidates))

    def test_core_add_until_target(self) -> None:
        positions = {"US.SPY": Position("US.SPY", 100, 10_000.0, 95.0)}
        candidates, _ = build_candidates(
            make_snapshot(), positions, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        spy = next(c for c in candidates if c.symbol == "US.SPY" and c.side == "BUY")
        self.assertEqual(spy.reason, "CORE_ADD")
        self.assertEqual(spy.quantity, 100)

    def test_satellite_add_requires_profit(self) -> None:
        positions = {"US.AAPL": Position("US.AAPL", 35, 3500.0, 110.0)}
        candidates, skipped = build_candidates(
            make_snapshot(), positions, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        reasons = {skip.symbol: skip.reason for skip in skipped}
        self.assertEqual(reasons["US.AAPL"], "ADD_NOT_PROFITABLE")
        self.assertFalse(
            any(c.symbol == "US.AAPL" and c.reason == "SATELLITE_ADD" for c in candidates)
        )

    def test_satellite_add_when_profitable(self) -> None:
        positions = {"US.AAPL": Position("US.AAPL", 35, 3500.0, 90.0)}
        candidates, _ = build_candidates(
            make_snapshot(), positions, 100_000.0, SETTINGS, WATCHLIST, allow_entries=True
        )
        add = next(c for c in candidates if c.symbol == "US.AAPL")
        self.assertEqual(add.reason, "SATELLITE_ADD")
        self.assertEqual(add.quantity, 35)


class TestCycle(unittest.TestCase):
    def test_buy_without_news_is_rejected(self) -> None:
        report = make_scout().run("09:45", NOW, make_snapshot(), make_account())
        spy = next(r for r in report.records if r.proposal.symbol == "US.SPY")
        self.assertEqual(spy.status, REJECTED)
        self.assertIn("R10_EVIDENCE_STALE", spy.gate.codes)

    def test_buy_needs_approval_then_ready(self) -> None:
        scout = make_scout()
        news = {symbol: fresh_news(symbol) for symbol in ("US.SPY", "US.QQQ", "US.IWM", "US.AAPL")}
        first = scout.run("09:45", NOW, make_snapshot(), make_account(), news=news)
        spy = next(r for r in first.records if r.proposal.symbol == "US.SPY")
        self.assertEqual(spy.status, AWAITING_APPROVAL)
        self.assertEqual(spy.gate.codes, ("R11_NO_APPROVAL",))
        self.assertEqual(spy.proposal.ticket_id, "P-20261117-0945-SPY-01")

        approvals = {
            spy.proposal.ticket_id: Approval(
                ticket_id=spy.proposal.ticket_id,
                proposal_hash=spy.proposal.canonical_hash(),
                approved_at=NOW,
            )
        }
        second = scout.run("09:45", NOW, make_snapshot(), make_account(), news=news, approvals=approvals)
        spy_ready = next(r for r in second.records if r.proposal.symbol == "US.SPY")
        self.assertEqual(spy_ready.status, READY)
        self.assertEqual(len(second.ready()), 1)
        self.assertEqual(second.canonical(), scout.run(
            "09:45", NOW, make_snapshot(), make_account(), news=news, approvals=approvals
        ).canonical())

    def test_approval_hash_mismatch_blocks(self) -> None:
        scout = make_scout()
        news = {"US.SPY": fresh_news("US.SPY")}
        first = scout.run("09:45", NOW, make_snapshot(), make_account(), news=news)
        spy = next(r for r in first.records if r.proposal.symbol == "US.SPY")
        approvals = {
            spy.proposal.ticket_id: Approval(
                ticket_id=spy.proposal.ticket_id,
                proposal_hash="deadbeef",
                approved_at=NOW,
            )
        }
        second = scout.run("09:45", NOW, make_snapshot(), make_account(), news=news, approvals=approvals)
        spy_again = next(r for r in second.records if r.proposal.symbol == "US.SPY")
        self.assertEqual(spy_again.gate.codes, ("R11_NO_APPROVAL",))
        self.assertEqual(second.ready(), ())

    def test_not_trading_day(self) -> None:
        thanksgiving = datetime(2026, 11, 26, 14, 45, tzinfo=timezone.utc)
        report = make_scout().run("09:45", thanksgiving, make_snapshot(), make_account())
        self.assertFalse(report.trading_day)
        self.assertEqual(report.records, ())
        self.assertEqual(report.skipped[0].reason, "NOT_TRADING_DAY")

    def test_1530_slot_is_exits_only(self) -> None:
        positions = {"US.AAPL": Position("US.AAPL", 10, 900.0, 100.0)}
        snapshot = make_snapshot(
            **{"US.AAPL": make_metrics("US.AAPL", 90.0, 95.0, 95.0, 110.0)}
        )
        report = make_scout().run("15:30", NOW, snapshot, make_account(positions=positions))
        self.assertFalse(report.entries_allowed)
        self.assertTrue(all(r.proposal.side == "SELL" for r in report.records))
        self.assertTrue(
            any(s.reason == "ENTRIES_BLOCKED_LAST_30_MIN" for s in report.skipped)
        )

    def test_sell_proposal_is_awaiting_approval(self) -> None:
        positions = {"US.AAPL": Position("US.AAPL", 10, 900.0, 100.0)}
        snapshot = make_snapshot(
            **{"US.AAPL": make_metrics("US.AAPL", 90.0, 95.0, 95.0, 110.0)}
        )
        report = make_scout().run("09:45", NOW, snapshot, make_account(positions=positions))
        sell = next(r for r in report.records if r.proposal.side == "SELL")
        self.assertEqual(sell.status, AWAITING_APPROVAL)
        self.assertEqual(sell.proposal.quantity, 10)

    def test_daily_stop_blocks_entries(self) -> None:
        account = replace(make_account(), daily_pnl_pct=-0.03)
        news = {symbol: fresh_news(symbol) for symbol in ("US.SPY", "US.QQQ", "US.IWM", "US.AAPL")}
        report = make_scout().run("09:45", NOW, make_snapshot(), account, news=news)
        for record in report.records:
            if record.proposal.side == "BUY":
                self.assertIn("R8_DAILY_STOP", record.gate.codes)


class TestAccountIO(unittest.TestCase):
    def test_load_account_and_approvals_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_path = Path(tmp) / "account.json"
            account_path.write_text(
                json.dumps(
                    {
                        "cash": 90000,
                        "equity": 100000,
                        "day_open_equity": 98000,
                        "executed_orders_today": 3,
                        "positions": {
                            "US.SPY": {"quantity": 10, "market_value": 1000, "average_cost": 95}
                        },
                        "working_orders": [{"symbol": "US.SPY", "side": "BUY"}],
                    }
                ),
                encoding="utf-8",
            )
            account = load_account(account_path, NOW)
            self.assertEqual(account.cash, 90000)
            self.assertAlmostEqual(account.daily_pnl_pct, 100000 / 98000 - 1)
            self.assertEqual(account.positions["US.SPY"].average_cost, 95.0)
            self.assertEqual(account.working_orders[0].symbol, "US.SPY")

            approvals_path = Path(tmp) / "approvals.json"
            approval = Approval("P-1", "hash", NOW, NOW + timedelta(minutes=30))
            save_approval(approvals_path, approval)
            loaded = load_approvals(approvals_path)
            self.assertEqual(loaded["P-1"].proposal_hash, "hash")
            self.assertEqual(loaded["P-1"].expires_at, NOW + timedelta(minutes=30))


if __name__ == "__main__":
    unittest.main()
