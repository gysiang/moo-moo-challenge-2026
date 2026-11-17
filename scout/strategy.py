from __future__ import annotations

from dataclasses import dataclass

from gate.models import Position
from scout.models import Metrics, Skip, Snapshot
from scout.settings import StrategySettings
from scout.watchlist import Watchlist


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: str
    quantity: int
    sleeve: str
    reason: str


def build_candidates(
    snapshot: Snapshot,
    positions: dict[str, Position],
    equity: float,
    settings: StrategySettings,
    watchlist: Watchlist,
    allow_entries: bool,
) -> tuple[list[Candidate], list[Skip]]:
    if equity <= 0:
        raise ValueError("equity must be positive")
    candidates: list[Candidate] = []
    skipped: list[Skip] = []
    core_symbols = {holding.symbol for holding in watchlist.core}
    satellite_set = set(watchlist.satellites)

    for symbol in sorted(positions):
        position = positions[symbol]
        if position.quantity <= 0:
            continue
        metrics = snapshot.metrics.get(symbol)
        if metrics is None:
            skipped.append(Skip(symbol, "NO_METRICS"))
            continue
        if symbol in core_symbols:
            reason = "EXIT_CORE_BELOW_SMA50" if metrics.last_price < metrics.sma50 else None
        else:
            pnl_pct = (
                metrics.last_price / position.average_cost - 1
                if position.average_cost > 0
                else 0.0
            )
            if metrics.last_price < metrics.sma50:
                reason = "EXIT_BELOW_SMA50"
            elif pnl_pct <= settings.satellite_exit_drawdown_pct:
                reason = "EXIT_STOP_LOSS"
            elif metrics.sma20 < metrics.sma50:
                reason = "EXIT_TREND_CROSS"
            else:
                reason = None
        if reason is not None:
            candidates.append(
                Candidate(symbol, "SELL", position.quantity, "exit", reason)
            )

    if not allow_entries:
        skipped.append(Skip("*", "ENTRIES_BLOCKED_LAST_30_MIN"))
        return candidates, skipped

    for holding in watchlist.core:
        metrics = snapshot.metrics.get(holding.symbol)
        if metrics is None:
            skipped.append(Skip(holding.symbol, "NO_METRICS"))
            continue
        held = positions.get(holding.symbol)
        held_qty = held.quantity if held else 0
        weight = held_qty * metrics.last_price / equity
        gap = holding.target_pct - weight
        if gap < settings.min_weight_gap_pct:
            skipped.append(Skip(holding.symbol, "CORE_AT_TARGET"))
            continue
        if not metrics.trend_up:
            skipped.append(Skip(holding.symbol, "CORE_TREND_DOWN"))
            continue
        tranche_value = equity * holding.target_pct / settings.core_tranches
        quantity = int(min(tranche_value, gap * equity) // metrics.last_price)
        if quantity < 1:
            skipped.append(Skip(holding.symbol, "QTY_ZERO"))
            continue
        reason = "CORE_ADD" if held_qty else "CORE_ENTRY"
        candidates.append(Candidate(holding.symbol, "BUY", quantity, "core", reason))

    tranche_value = equity * settings.satellite_tranche_pct
    held_satellites = sorted(
        symbol
        for symbol, position in positions.items()
        if symbol in satellite_set and position.quantity > 0
    )
    for symbol in held_satellites:
        metrics = snapshot.metrics.get(symbol)
        if metrics is None:
            skipped.append(Skip(symbol, "NO_METRICS"))
            continue
        held = positions[symbol]
        pnl_pct = (
            metrics.last_price / held.average_cost - 1 if held.average_cost > 0 else 0.0
        )
        if pnl_pct <= 0:
            skipped.append(Skip(symbol, "ADD_NOT_PROFITABLE"))
            continue
        if metrics.last_price <= metrics.sma50:
            skipped.append(Skip(symbol, "ADD_BELOW_SMA50"))
            continue
        tranches = int((held.quantity * metrics.last_price) // tranche_value)
        if tranches >= settings.satellite_max_tranches:
            skipped.append(Skip(symbol, "MAX_TRANCHES"))
            continue
        quantity = int(tranche_value // metrics.last_price)
        if quantity < 1:
            skipped.append(Skip(symbol, "QTY_ZERO"))
            continue
        candidates.append(Candidate(symbol, "BUY", quantity, "satellite", "SATELLITE_ADD"))

    open_slots = settings.satellite_max_names - len(held_satellites)
    entries: list[Metrics] = []
    for symbol in watchlist.satellites:
        if symbol in positions:
            continue
        metrics = snapshot.metrics.get(symbol)
        if metrics is None:
            skipped.append(Skip(symbol, "NO_METRICS"))
            continue
        if not metrics.trend_up:
            skipped.append(Skip(symbol, "TREND_DOWN"))
            continue
        if metrics.last_price < metrics.high20 * (1 - settings.satellite_near_high_pct):
            skipped.append(Skip(symbol, "NOT_NEAR_HIGH"))
            continue
        entries.append(metrics)

    entries.sort(key=lambda item: (-item.roc20, item.symbol))
    for index, metrics in enumerate(entries):
        if index >= open_slots:
            skipped.append(Skip(metrics.symbol, "NO_SLOT"))
            continue
        quantity = int(tranche_value // metrics.last_price)
        if quantity < 1:
            skipped.append(Skip(metrics.symbol, "QTY_ZERO"))
            continue
        candidates.append(
            Candidate(metrics.symbol, "BUY", quantity, "satellite", "SATELLITE_ENTRY")
        )

    return candidates, skipped
