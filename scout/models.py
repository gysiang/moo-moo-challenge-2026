from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from gate import GateResult, Proposal
from gate.models import as_utc

READY = "READY"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class Metrics:
    symbol: str
    instrument_type: str
    last_price: float
    market_cap: float
    sma20: float
    sma50: float
    high20: float
    roc20: float
    as_of: datetime
    us_listed: bool = True
    tradable: bool = True

    def __post_init__(self) -> None:
        as_utc(self.as_of)

    @property
    def trend_up(self) -> bool:
        return self.last_price > self.sma50 and self.sma20 > self.sma50


@dataclass(frozen=True)
class Snapshot:
    as_of: datetime
    metrics: dict[str, Metrics]

    def __post_init__(self) -> None:
        as_utc(self.as_of)


@dataclass(frozen=True)
class Skip:
    symbol: str
    reason: str


@dataclass(frozen=True)
class Record:
    sleeve: str
    proposal: Proposal
    gate: GateResult
    status: str


@dataclass(frozen=True)
class CycleReport:
    slot: str
    generated_at: datetime
    trading_day: bool
    entries_allowed: bool
    records: tuple[Record, ...]
    skipped: tuple[Skip, ...]

    def ready(self) -> tuple[Proposal, ...]:
        return tuple(r.proposal for r in self.records if r.status == READY)

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "generated_at": self.generated_at.isoformat(),
            "trading_day": self.trading_day,
            "entries_allowed": self.entries_allowed,
            "records": [
                {
                    "sleeve": record.sleeve,
                    "status": record.status,
                    "ticket_id": record.proposal.ticket_id,
                    "proposal_hash": record.proposal.canonical_hash(),
                    "symbol": record.proposal.symbol,
                    "side": record.proposal.side,
                    "quantity": record.proposal.quantity,
                    "order_type": record.proposal.order_type,
                    "limit_price": record.proposal.limit_price,
                    "est_cost": record.proposal.est_cost,
                    "created_at": record.proposal.created_at.isoformat(),
                    "rationale": record.proposal.rationale,
                    "evidence": [
                        {
                            "id": item.evidence_id,
                            "kind": item.kind,
                            "as_of": item.as_of.isoformat(),
                            "source": item.source,
                        }
                        for item in record.proposal.evidence
                    ],
                    "gate": {
                        "passed": record.gate.passed,
                        "codes": list(record.gate.codes),
                        "checked": list(record.gate.checked),
                    },
                }
                for record in self.records
            ],
            "skipped": [
                {"symbol": skip.symbol, "reason": skip.reason} for skip in self.skipped
            ],
        }

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
