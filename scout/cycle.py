from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Optional

from gate import Approval, Evidence, ExitMandate, Instrument, Proposal, evaluate
from gate.config import Limits
from gate.models import AccountState

from scout.models import (
    AWAITING_APPROVAL,
    READY,
    REJECTED,
    CycleReport,
    Record,
    Skip,
    Snapshot,
)
from scout.settings import StrategySettings
from scout.slots import allows_entries, eastern, is_trading_day
from scout.strategy import build_candidates
from scout.watchlist import Watchlist

MAX_NEWS_ITEMS = 3


class Scout:
    def __init__(
        self,
        limits: Limits,
        settings: StrategySettings,
        watchlist: Watchlist,
        mandate: Optional[ExitMandate] = None,
    ) -> None:
        self._limits = limits
        self._settings = settings
        self._watchlist = watchlist
        self._mandate = mandate

    def run(
        self,
        slot: str,
        now: datetime,
        snapshot: Snapshot,
        account: AccountState,
        approvals: Optional[dict[str, Approval]] = None,
        news: Optional[dict[str, tuple[Evidence, ...]]] = None,
        allow_entries: Optional[bool] = None,
    ) -> CycleReport:
        local = eastern(now)
        day = local.date()
        if not is_trading_day(day):
            return CycleReport(
                slot=slot,
                generated_at=now,
                trading_day=False,
                entries_allowed=False,
                records=(),
                skipped=(Skip("*", "NOT_TRADING_DAY"),),
            )

        entries_ok = (
            allows_entries(slot, day) if allow_entries is None else allow_entries
        )
        approvals = approvals or {}
        news = news or {}
        candidates, skipped = build_candidates(
            snapshot=snapshot,
            positions=account.positions,
            equity=account.equity,
            settings=self._settings,
            watchlist=self._watchlist,
            allow_entries=entries_ok,
        )

        records: list[Record] = []
        bare_slot = slot.replace(":", "")
        mandated_exits = 0
        for index, candidate in enumerate(candidates, start=1):
            metrics = snapshot.metrics[candidate.symbol]
            bare_symbol = candidate.symbol.split(".", 1)[-1]
            ticket_id = (
                f"P-{local:%Y%m%d}-{bare_slot}-{bare_symbol}-{index:02d}"
            )
            price = round(metrics.last_price, 2)
            evidence: list[Evidence] = [
                Evidence(
                    evidence_id=f"Q-{ticket_id}",
                    kind="quote",
                    as_of=snapshot.as_of,
                    source="moomoo.quote_cur_kline",
                )
            ]
            evidence.extend(news.get(candidate.symbol, ())[:MAX_NEWS_ITEMS])
            proposal = Proposal(
                ticket_id=ticket_id,
                created_at=now,
                symbol=candidate.symbol,
                side=candidate.side,
                quantity=candidate.quantity,
                order_type="LIMIT",
                limit_price=price,
                est_cost=round(candidate.quantity * price, 2),
                starter_tranche=1,
                rationale=(
                    f"{candidate.reason}: last {metrics.last_price:.2f}, "
                    f"ema{self._settings.trend_fast_ema} {metrics.ema_fast:.2f}, "
                    f"ema{self._settings.trend_slow_ema} {metrics.ema_slow:.2f}, "
                    f"sma50 {metrics.sma50:.2f}, roc20 {metrics.roc20:+.2%}"
                ),
                evidence=tuple(evidence),
                exit_reason=candidate.reason if candidate.side == "SELL" else "",
            )
            instrument = Instrument(
                symbol=metrics.symbol,
                instrument_type=metrics.instrument_type,
                us_listed=metrics.us_listed,
                last_price=metrics.last_price,
                market_cap=metrics.market_cap,
                tradable=metrics.tradable,
            )
            state = replace(
                account,
                now=now,
                approval=approvals.get(ticket_id),
                mandated_exits_this_run=mandated_exits,
            )
            gate = evaluate(proposal, instrument, state, self._limits, self._mandate)
            if gate.passed:
                status = READY
                if self._mandate is not None and proposal.exit_reason:
                    mandated_exits += 1
            elif gate.codes == ("R11_NO_APPROVAL",):
                status = AWAITING_APPROVAL
            else:
                status = REJECTED
            records.append(Record(candidate.sleeve, proposal, gate, status))

        return CycleReport(
            slot=slot,
            generated_at=now,
            trading_day=True,
            entries_allowed=entries_ok,
            records=tuple(records),
            skipped=tuple(skipped),
            mandate_id=self._mandate.mandate_id if self._mandate else "",
            mandate_max_exits=self._mandate.max_exits_per_run if self._mandate else 0,
        )
