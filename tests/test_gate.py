from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate import (
    AccountState,
    Approval,
    Evidence,
    Instrument,
    Position,
    Proposal,
    WorkingOrder,
    evaluate,
    load_limits,
    validate_proposal,
)

CONFIG = Path(__file__).resolve().parent.parent / "config.json"
LIMITS = load_limits(CONFIG)
NOW = datetime(2026, 11, 18, 14, 45, tzinfo=timezone.utc)


def fresh_news() -> Evidence:
    return Evidence("E2", "news", NOW - timedelta(hours=2), "moomoo")


def fresh_quote() -> Evidence:
    return Evidence("E1", "quote", NOW - timedelta(minutes=5), "moomoo")


def make_proposal(**overrides) -> Proposal:
    base = Proposal(
        ticket_id="P-20261118-1445-AAPL-01",
        created_at=NOW,
        symbol="US.AAPL",
        side="BUY",
        quantity=14,
        order_type="LIMIT",
        limit_price=231.40,
        est_cost=3239.60,
        starter_tranche=1,
        rationale="20-day high with trend alignment",
        evidence=(fresh_quote(), fresh_news()),
    )
    return replace(base, **overrides)


def make_instrument(**overrides) -> Instrument:
    base = Instrument(
        symbol="US.AAPL",
        instrument_type="stock",
        us_listed=True,
        last_price=231.40,
        market_cap=3_500_000_000_000,
    )
    return replace(base, **overrides)


def approve(proposal: Proposal, **overrides) -> Approval:
    base = Approval(
        ticket_id=proposal.ticket_id,
        proposal_hash=proposal.canonical_hash(),
        approved_at=NOW - timedelta(minutes=1),
    )
    return replace(base, **overrides)


def make_state(proposal: Proposal | None = None, **overrides) -> AccountState:
    base = AccountState(
        now=NOW,
        cash=100_000.0,
        equity=100_000.0,
        positions={},
        working_orders=(),
        executed_orders_today=3,
        daily_pnl_pct=0.004,
        approval=approve(proposal or make_proposal()),
    )
    return replace(base, **overrides)


class TestGatePasses(unittest.TestCase):
    def test_clean_proposal_passes(self) -> None:
        proposal = make_proposal()
        result = evaluate(proposal, make_instrument(), make_state(proposal), LIMITS)
        self.assertTrue(result.passed, result.rejections)
        self.assertEqual(result.first_failure, None)
        self.assertEqual(len(result.checked), 11)

    def test_gate_is_deterministic(self) -> None:
        proposal = make_proposal()
        args = (proposal, make_instrument(), make_state(proposal), LIMITS)
        self.assertEqual(evaluate(*args), evaluate(*args))


class TestCompetitionAcceptance(unittest.TestCase):
    def test_t1_penny_stock_rejected(self) -> None:
        proposal = make_proposal(limit_price=0.80, quantity=100, est_cost=80.0)
        result = evaluate(
            proposal,
            make_instrument(last_price=0.80, market_cap=2_000_000_000),
            make_state(proposal),
            LIMITS,
        )
        self.assertIn("R2_SIZE", result.codes)

    def test_t2_concentration_rejected(self) -> None:
        proposal = make_proposal()
        state = make_state(
            proposal,
            positions={
                "US.AAPL": Position("US.AAPL", 177, 41_000.0, 220.0),
            },
        )
        result = evaluate(proposal, make_instrument(), state, LIMITS)
        self.assertIn("R5_CONCENTRATION", result.codes)

    def test_t3_expired_approval_rejected(self) -> None:
        proposal = make_proposal()
        expired = approve(proposal, approved_at=NOW - LIMITS.approval_ttl - timedelta(minutes=1))
        result = evaluate(
            proposal, make_instrument(), make_state(proposal, approval=expired), LIMITS
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_overnight_approval_survives_twelve_hour_ttl(self) -> None:
        proposal = make_proposal()
        overnight = approve(proposal, approved_at=NOW - timedelta(hours=11))
        result = evaluate(
            proposal, make_instrument(), make_state(proposal, approval=overnight), LIMITS
        )
        self.assertNotIn("R11_NO_APPROVAL", result.codes)

    def test_approval_older_than_ttl_is_rejected(self) -> None:
        proposal = make_proposal()
        stale = approve(proposal, approved_at=NOW - timedelta(hours=13))
        result = evaluate(
            proposal, make_instrument(), make_state(proposal, approval=stale), LIMITS
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_t4_missing_news_evidence_rejected(self) -> None:
        proposal = make_proposal(evidence=(fresh_quote(),))
        result = evaluate(proposal, make_instrument(), make_state(proposal), LIMITS)
        self.assertIn("R10_EVIDENCE_STALE", result.codes)

    def test_t5_duplicate_working_order_rejected(self) -> None:
        proposal = make_proposal()
        state = make_state(
            proposal, working_orders=(WorkingOrder("US.AAPL", "BUY"),)
        )
        result = evaluate(proposal, make_instrument(), state, LIMITS)
        self.assertIn("R9_DUPLICATE", result.codes)
        self.assertNotIn("R11_NO_APPROVAL", result.codes)


class TestIndividualRules(unittest.TestCase):
    def test_r1_non_us_listed(self) -> None:
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(us_listed=False),
            make_state(proposal),
            LIMITS,
        )
        self.assertIn("R1_UNIVERSE", result.codes)

    def test_r1_untradable(self) -> None:
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(tradable=False),
            make_state(proposal),
            LIMITS,
        )
        self.assertIn("R1_UNIVERSE", result.codes)

    def test_r3_option_rejected(self) -> None:
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(instrument_type="option"),
            make_state(proposal),
            LIMITS,
        )
        self.assertIn("R3_TYPE", result.codes)

    def test_r4_insufficient_cash(self) -> None:
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(),
            make_state(proposal, cash=100.0),
            LIMITS,
        )
        self.assertIn("R4_FUNDS", result.codes)

    def test_r4_sell_exceeds_inventory(self) -> None:
        proposal = make_proposal(side="SELL", quantity=10, evidence=(fresh_quote(),))
        state = make_state(
            proposal,
            positions={"US.AAPL": Position("US.AAPL", 5, 1_157.0, 231.40)},
        )
        result = evaluate(proposal, make_instrument(), state, LIMITS)
        self.assertIn("R4_FUNDS", result.codes)

    def test_r6_position_count_cap(self) -> None:
        held = {
            f"US.SYM{i}": Position(f"US.SYM{i}", 1, 1_000.0, 1_000.0)
            for i in range(12)
        }
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(),
            make_state(proposal, positions=held),
            LIMITS,
        )
        self.assertIn("R6_POSITION_COUNT", result.codes)

    def test_r6_existing_position_does_not_consume_slot(self) -> None:
        held = {
            f"US.SYM{i}": Position(f"US.SYM{i}", 1, 1_000.0, 1_000.0)
            for i in range(11)
        }
        held["US.AAPL"] = Position("US.AAPL", 1, 231.40, 231.40)
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(),
            make_state(proposal, positions=held),
            LIMITS,
        )
        self.assertNotIn("R6_POSITION_COUNT", result.codes)

    def test_r7_order_count_cap(self) -> None:
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(),
            make_state(proposal, executed_orders_today=20),
            LIMITS,
        )
        self.assertIn("R7_ORDER_COUNT", result.codes)

    def test_r8_daily_stop_blocks_buy(self) -> None:
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(),
            make_state(proposal, daily_pnl_pct=-0.025),
            LIMITS,
        )
        self.assertIn("R8_DAILY_STOP", result.codes)

    def test_r8_daily_stop_allows_sell(self) -> None:
        proposal = make_proposal(side="SELL", quantity=5, evidence=(fresh_quote(),))
        state = make_state(
            proposal,
            daily_pnl_pct=-0.025,
            positions={"US.AAPL": Position("US.AAPL", 5, 1_157.0, 231.40)},
        )
        result = evaluate(proposal, make_instrument(), state, LIMITS)
        self.assertNotIn("R8_DAILY_STOP", result.codes)
        self.assertTrue(result.passed, result.rejections)

    def test_r9_opposite_side_is_not_duplicate(self) -> None:
        proposal = make_proposal()
        state = make_state(
            proposal, working_orders=(WorkingOrder("US.AAPL", "SELL"),)
        )
        result = evaluate(proposal, make_instrument(), state, LIMITS)
        self.assertNotIn("R9_DUPLICATE", result.codes)

    def test_r10_stale_evidence(self) -> None:
        stale = Evidence("E9", "quote", NOW - timedelta(hours=25), "moomoo")
        proposal = make_proposal(evidence=(stale, fresh_news()))
        result = evaluate(proposal, make_instrument(), make_state(proposal), LIMITS)
        self.assertIn("R10_EVIDENCE_STALE", result.codes)

    def test_r10_unsourced_evidence(self) -> None:
        bad = Evidence("E9", "news", NOW - timedelta(hours=1), "")
        proposal = make_proposal(evidence=(fresh_quote(), bad))
        result = evaluate(proposal, make_instrument(), make_state(proposal), LIMITS)
        self.assertIn("R10_EVIDENCE_STALE", result.codes)

    def test_r11_missing_approval(self) -> None:
        proposal = make_proposal()
        result = evaluate(
            proposal,
            make_instrument(),
            make_state(proposal, approval=None),
            LIMITS,
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_r11_ticket_mismatch(self) -> None:
        proposal = make_proposal()
        other = make_proposal(ticket_id="P-OTHER")
        result = evaluate(
            proposal,
            make_instrument(),
            make_state(proposal, approval=approve(other)),
            LIMITS,
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)

    def test_r11_size_change_after_approval(self) -> None:
        proposal = make_proposal()
        original = approve(proposal)
        resized = replace(proposal, quantity=20)
        result = evaluate(
            resized,
            make_instrument(),
            make_state(proposal, approval=original),
            LIMITS,
        )
        self.assertIn("R11_NO_APPROVAL", result.codes)
        self.assertIn("changed after approval", result.first_failure.message)


class TestStructuralValidation(unittest.TestCase):
    def test_zero_quantity_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_proposal(make_proposal(quantity=0))

    def test_non_positive_price_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_proposal(make_proposal(limit_price=0))

    def test_market_order_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_proposal(make_proposal(order_type="MARKET"))

    def test_bad_side_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_proposal(make_proposal(side="SHORT"))


if __name__ == "__main__":
    unittest.main()
