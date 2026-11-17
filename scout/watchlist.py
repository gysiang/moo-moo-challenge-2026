from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoreHolding:
    symbol: str
    target_pct: float


@dataclass(frozen=True)
class Watchlist:
    core: tuple[CoreHolding, ...]
    satellites: tuple[str, ...]


def load_watchlist(path: Path) -> Watchlist:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    core = tuple(
        CoreHolding(symbol=row["symbol"], target_pct=float(row["target_pct"]))
        for row in payload["core"]
    )
    satellites = tuple(payload["satellites"])
    if len(core) > 3:
        raise ValueError("core sleeve supports at most 3 ETFs (PRD §7A)")
    if len(set(satellites)) != len(satellites):
        raise ValueError("duplicate satellite symbols")
    if abs(sum(item.target_pct for item in core) - 0.60) > 1e-9:
        raise ValueError("core targets must sum to 60% of equity")
    return Watchlist(core=core, satellites=satellites)
