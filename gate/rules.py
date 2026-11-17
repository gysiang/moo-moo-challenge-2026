from __future__ import annotations

from typing import Optional

from gate.config import Limits
from gate.models import (
    AccountState,
    GateResult,
    Instrument,
    Proposal,
    Rejection,
)

EPSILON = 1e-9
CLOCK_SKEW_SECONDS = 300


def estimate_fees(notional: float, limits: Limits) -> float:
    return max(limits.min_fee, abs(notional) * limits.fee_bps / 10_000)


def validate_proposal(proposal: Proposal) -> None:
    if proposal.side not in ("BUY", "SELL"):
        raise ValueError(f"unsupported side: {proposal.side!r}")
    if proposal.order_type != "LIMIT":
        raise ValueError(f"MVP supports LIMIT orders only, got {proposal.order_type!r}")
    if proposal.quantity <= 0:
        raise ValueError(f"quantity must be positive, got {proposal.quantity}")
    if proposal.limit_price <= 0:
        raise ValueError(f"limit_price must be positive, got {proposal.limit_price}")
    if proposal.est_cost < 0:
        raise ValueError(f"est_cost must be non-negative, got {proposal.est_cost}")
    if not proposal.ticket_id:
        raise ValueError("ticket_id is required")


def evaluate(
    proposal: Proposal,
    instrument: Optional[Instrument],
    state: AccountState,
    limits: Limits,
) -> GateResult:
    validate_proposal(proposal)
    if state.equity <= 0:
        raise ValueError(f"equity must be positive, got {state.equity}")

    checks: list[tuple[str, Optional[Rejection]]] = [
        ("R1", _r1_universe(instrument)),
        ("R2", _r2_size(instrument, limits)),
        ("R3", _r3_type(instrument, limits)),
        ("R4", _r4_cash_and_inventory(proposal, state, limits)),
        ("R5", _r5_concentration(proposal, state, limits)),
        ("R6", _r6_position_count(proposal, state, limits)),
        ("R7", _r7_order_count(state, limits)),
        ("R8", _r8_daily_stop(proposal, state, limits)),
        ("R9", _r9_duplicate(proposal, state)),
        ("R10", _r10_evidence(proposal, state, limits)),
        ("R11", _r11_approval(proposal, state, limits)),
    ]
    rejections = tuple(r for _, r in checks if r is not None)
    return GateResult(
        passed=not rejections,
        rejections=rejections,
        checked=tuple(code for code, _ in checks),
    )


def _r1_universe(instrument: Optional[Instrument]) -> Optional[Rejection]:
    if instrument is None:
        return Rejection("R1_UNIVERSE", "instrument not found in the universe")
    if not instrument.us_listed:
        return Rejection("R1_UNIVERSE", f"{instrument.symbol} is not US-listed")
    if not instrument.tradable:
        return Rejection("R1_UNIVERSE", f"{instrument.symbol} is not tradable")
    return None


def _r2_size(instrument: Optional[Instrument], limits: Limits) -> Optional[Rejection]:
    if instrument is None:
        return None
    if instrument.last_price < limits.min_price:
        return Rejection(
            "R2_SIZE",
            f"price {instrument.last_price:.2f} below minimum {limits.min_price:.2f}",
        )
    if instrument.market_cap < limits.min_market_cap:
        return Rejection(
            "R2_SIZE",
            f"market cap {instrument.market_cap:.0f} below minimum {limits.min_market_cap:.0f}",
        )
    return None


def _r3_type(instrument: Optional[Instrument], limits: Limits) -> Optional[Rejection]:
    if instrument is None:
        return None
    if instrument.instrument_type not in limits.allowed_instrument_types:
        return Rejection(
            "R3_TYPE",
            f"{instrument.instrument_type} not in {sorted(limits.allowed_instrument_types)}",
        )
    return None


def _r4_cash_and_inventory(
    proposal: Proposal, state: AccountState, limits: Limits
) -> Optional[Rejection]:
    notional = proposal.quantity * proposal.limit_price
    if proposal.side == "BUY":
        required = notional + estimate_fees(notional, limits)
        if state.cash + EPSILON < required:
            return Rejection(
                "R4_FUNDS",
                f"cash {state.cash:.2f} below required {required:.2f}",
            )
        return None
    held = state.positions.get(proposal.symbol)
    held_qty = held.quantity if held else 0
    if held_qty < proposal.quantity:
        return Rejection(
            "R4_FUNDS",
            f"sell quantity {proposal.quantity} exceeds held {held_qty}",
        )
    return None


def _r5_concentration(
    proposal: Proposal, state: AccountState, limits: Limits
) -> Optional[Rejection]:
    if proposal.side != "BUY":
        return None
    existing = state.positions.get(proposal.symbol)
    existing_mv = existing.market_value if existing else 0.0
    post_mv = existing_mv + proposal.quantity * proposal.limit_price
    pct = post_mv / state.equity
    if pct > limits.max_single_name_pct + EPSILON:
        return Rejection(
            "R5_CONCENTRATION",
            f"{proposal.symbol} would be {pct:.1%} of equity, limit {limits.max_single_name_pct:.0%}",
        )
    return None


def _r6_position_count(
    proposal: Proposal, state: AccountState, limits: Limits
) -> Optional[Rejection]:
    if proposal.side != "BUY":
        return None
    if proposal.symbol in state.positions:
        return None
    post_count = len(state.positions) + 1
    if post_count > limits.max_positions:
        return Rejection(
            "R6_POSITION_COUNT",
            f"new position would make {post_count}, limit {limits.max_positions}",
        )
    return None


def _r7_order_count(state: AccountState, limits: Limits) -> Optional[Rejection]:
    post_count = state.executed_orders_today + 1
    if post_count > limits.max_orders_per_day:
        return Rejection(
            "R7_ORDER_COUNT",
            f"order {post_count} today, limit {limits.max_orders_per_day}",
        )
    return None


def _r8_daily_stop(
    proposal: Proposal, state: AccountState, limits: Limits
) -> Optional[Rejection]:
    if proposal.side != "BUY":
        return None
    if state.daily_pnl_pct <= limits.daily_stop_pct + EPSILON:
        return Rejection(
            "R8_DAILY_STOP",
            f"daily P&L {state.daily_pnl_pct:.2%} at stop {limits.daily_stop_pct:.2%}",
        )
    return None


def _r9_duplicate(proposal: Proposal, state: AccountState) -> Optional[Rejection]:
    for order in state.working_orders:
        if order.symbol == proposal.symbol and order.side == proposal.side:
            return Rejection(
                "R9_DUPLICATE",
                f"working {order.side} order already exists for {order.symbol}",
            )
    return None


def _r10_evidence(
    proposal: Proposal, state: AccountState, limits: Limits
) -> Optional[Rejection]:
    if not proposal.evidence:
        return Rejection("R10_EVIDENCE_STALE", "no evidence supplied")
    horizon = limits.evidence_max_age
    for item in proposal.evidence:
        if not item.source:
            return Rejection(
                "R10_EVIDENCE_STALE", f"evidence {item.evidence_id} has no source"
            )
        age = state.now - item.as_of
        if age > horizon:
            return Rejection(
                "R10_EVIDENCE_STALE",
                f"evidence {item.evidence_id} is {age.total_seconds() / 3600:.1f}h old, max {horizon.total_seconds() / 3600:.0f}h",
            )
        if age.total_seconds() < -CLOCK_SKEW_SECONDS:
            return Rejection(
                "R10_EVIDENCE_STALE",
                f"evidence {item.evidence_id} is timestamped in the future",
            )
    if proposal.side == "BUY" and not any(i.kind == "news" for i in proposal.evidence):
        return Rejection("R10_EVIDENCE_STALE", "BUY requires at least one news item")
    return None


def _r11_approval(
    proposal: Proposal, state: AccountState, limits: Limits
) -> Optional[Rejection]:
    approval = state.approval
    if approval is None:
        return Rejection("R11_NO_APPROVAL", "no approval on record")
    if approval.ticket_id != proposal.ticket_id:
        return Rejection(
            "R11_NO_APPROVAL",
            f"approval is for {approval.ticket_id}, proposal is {proposal.ticket_id}",
        )
    if approval.proposal_hash != proposal.canonical_hash():
        return Rejection(
            "R11_NO_APPROVAL", "proposal changed after approval; re-approval required"
        )
    if (state.now - approval.approved_at).total_seconds() < -CLOCK_SKEW_SECONDS:
        return Rejection("R11_NO_APPROVAL", "approval timestamp is in the future")
    expiry = approval.expires_at or (approval.approved_at + limits.approval_ttl)
    if state.now >= expiry:
        return Rejection(
            "R11_NO_APPROVAL", f"approval expired at {expiry.isoformat()}"
        )
    return None
