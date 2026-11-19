from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from gate.models import AccountState, Proposal, Rejection, as_utc

EXIT_REASONS = frozenset(
    {
        "EXIT_CORE_BELOW_SMA50",
        "EXIT_BELOW_SMA50",
        "EXIT_STOP_LOSS",
        "EXIT_TREND_CROSS",
    }
)
CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True)
class ExitMandate:
    """Operator authorization for risk-reducing exits without a per-ticket approval."""

    mandate_id: str
    account_id: str
    granted_at: datetime
    expires_at: datetime
    allowed_reasons: frozenset[str]
    max_exits_per_run: int
    note: str = ""

    def __post_init__(self) -> None:
        as_utc(self.granted_at)
        as_utc(self.expires_at)
        if not self.mandate_id:
            raise ValueError("mandate_id is required")
        if not self.account_id:
            raise ValueError("account_id is required")
        if self.expires_at <= self.granted_at:
            raise ValueError("expires_at must be after granted_at")
        if self.max_exits_per_run < 1:
            raise ValueError("max_exits_per_run must be positive")
        unknown = set(self.allowed_reasons) - EXIT_REASONS
        if unknown:
            raise ValueError(f"unknown exit reasons: {sorted(unknown)}")
        if not self.allowed_reasons:
            raise ValueError("a mandate must allow at least one exit reason")

    def canonical_hash(self) -> str:
        payload = {
            "mandate_id": self.mandate_id,
            "account_id": self.account_id,
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "allowed_reasons": sorted(self.allowed_reasons),
            "max_exits_per_run": self.max_exits_per_run,
            "note": self.note,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "mandate_id": self.mandate_id,
            "account_id": self.account_id,
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "allowed_reasons": sorted(self.allowed_reasons),
            "max_exits_per_run": self.max_exits_per_run,
            "note": self.note,
            "mandate_hash": self.canonical_hash(),
        }

    def covers(self, proposal: Proposal, state: AccountState) -> Optional[Rejection]:
        if proposal.side != "SELL":
            return Rejection("R11_NO_APPROVAL", "a mandate only covers SELL exits")
        if proposal.exit_reason not in self.allowed_reasons:
            return Rejection(
                "R11_NO_APPROVAL",
                f"exit reason {proposal.exit_reason or '(none)'} is not mandated",
            )
        if state.account_id != self.account_id:
            return Rejection(
                "R11_NO_APPROVAL",
                f"mandate is for account {self.account_id}, state is {state.account_id or '(unset)'}",
            )
        if state.mandated_exits_this_run >= self.max_exits_per_run:
            return Rejection(
                "R11_NO_APPROVAL",
                f"mandate allows {self.max_exits_per_run} exits per run, "
                f"{state.mandated_exits_this_run} already used",
            )
        if (state.now - self.granted_at).total_seconds() < -CLOCK_SKEW_SECONDS:
            return Rejection("R11_NO_APPROVAL", "mandate is not granted yet")
        if state.now >= self.expires_at:
            return Rejection(
                "R11_NO_APPROVAL", f"mandate expired at {self.expires_at.isoformat()}"
            )
        held = state.positions.get(proposal.symbol)
        held_qty = held.quantity if held else 0
        if held_qty <= 0:
            return Rejection("R11_NO_APPROVAL", f"no held position in {proposal.symbol}")
        if proposal.quantity != held_qty:
            return Rejection(
                "R11_NO_APPROVAL",
                f"mandate covers full exits only: {proposal.quantity} != held {held_qty}",
            )
        return None


def load_mandate(path: str | Path) -> Optional[ExitMandate]:
    file = Path(path)
    if not file.exists():
        return None
    payload = json.loads(file.read_text(encoding="utf-8"))
    mandate = ExitMandate(
        mandate_id=payload["mandate_id"],
        account_id=str(payload["account_id"]),
        granted_at=datetime.fromisoformat(payload["granted_at"].replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")),
        allowed_reasons=frozenset(payload["allowed_reasons"]),
        max_exits_per_run=int(payload["max_exits_per_run"]),
        note=payload.get("note", ""),
    )
    stored = payload.get("mandate_hash")
    if stored != mandate.canonical_hash():
        raise ValueError(f"{file}: mandate hash mismatch; re-grant it instead of editing")
    return mandate


def save_mandate(path: str | Path, mandate: ExitMandate) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps(mandate.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )