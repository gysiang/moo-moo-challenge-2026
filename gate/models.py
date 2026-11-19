from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional

Side = Literal["BUY", "SELL"]


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str
    as_of: datetime
    source: str

    def __post_init__(self) -> None:
        as_utc(self.as_of)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    instrument_type: str
    us_listed: bool
    last_price: float
    market_cap: float
    tradable: bool = True


@dataclass(frozen=True)
class Proposal:
    ticket_id: str
    created_at: datetime
    symbol: str
    side: Side
    quantity: int
    order_type: str
    limit_price: float
    est_cost: float
    starter_tranche: int
    rationale: str = ""
    evidence: tuple[Evidence, ...] = ()
    exit_reason: str = ""

    def __post_init__(self) -> None:
        as_utc(self.created_at)

    def canonical_hash(self) -> str:
        payload = {
            "ticket_id": self.ticket_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": round(self.limit_price, 6),
            "est_cost": round(self.est_cost, 6),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    market_value: float
    average_cost: float


@dataclass(frozen=True)
class WorkingOrder:
    symbol: str
    side: Side


@dataclass(frozen=True)
class Approval:
    ticket_id: str
    proposal_hash: str
    approved_at: datetime
    expires_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        as_utc(self.approved_at)
        if self.expires_at is not None:
            as_utc(self.expires_at)


@dataclass(frozen=True)
class AccountState:
    now: datetime
    cash: float
    equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    working_orders: tuple[WorkingOrder, ...] = ()
    executed_orders_today: int = 0
    daily_pnl_pct: float = 0.0
    approval: Optional[Approval] = None
    account_id: str = ""
    mandated_exits_this_run: int = 0

    def __post_init__(self) -> None:
        as_utc(self.now)


@dataclass(frozen=True)
class Rejection:
    code: str
    message: str


@dataclass(frozen=True)
class GateResult:
    passed: bool
    rejections: tuple[Rejection, ...]
    checked: tuple[str, ...]

    @property
    def first_failure(self) -> Optional[Rejection]:
        return self.rejections[0] if self.rejections else None

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.rejections)
