from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StrategySettings:
    core_tranches: int = 3
    satellite_max_names: int = 6
    satellite_tranche_pct: float = 0.035
    satellite_max_tranches: int = 3
    satellite_near_high_pct: float = 0.05
    satellite_exit_drawdown_pct: float = -0.08
    min_weight_gap_pct: float = 0.01
    trend_fast_ema: int = 8
    trend_slow_ema: int = 21


def load_strategy(path: Path) -> StrategySettings:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    overrides = payload.get("strategy", {})
    allowed = StrategySettings.__dataclass_fields__.keys()
    unknown = set(overrides) - set(allowed)
    if unknown:
        raise ValueError(f"unknown strategy keys: {sorted(unknown)}")
    return StrategySettings(**overrides)
