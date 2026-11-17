from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    min_price: float
    min_market_cap: float
    allowed_instrument_types: frozenset[str]
    max_positions: int
    max_orders_per_day: int
    max_single_name_pct: float
    daily_stop_pct: float
    evidence_max_age: timedelta
    approval_ttl: timedelta
    fee_bps: float
    min_fee: float


def load_limits(path: str | Path) -> Limits:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Limits(
        min_price=float(raw["min_price"]),
        min_market_cap=float(raw["min_market_cap"]),
        allowed_instrument_types=frozenset(raw["allowed_instrument_types"]),
        max_positions=int(raw["max_positions"]),
        max_orders_per_day=int(raw["max_orders_per_day"]),
        max_single_name_pct=float(raw["max_single_name_pct"]),
        daily_stop_pct=float(raw["daily_stop_pct"]),
        evidence_max_age=timedelta(hours=float(raw["evidence_max_age_hours"])),
        approval_ttl=timedelta(minutes=float(raw["approval_ttl_minutes"])),
        fee_bps=float(raw["fee_bps"]),
        min_fee=float(raw["min_fee"]),
    )
