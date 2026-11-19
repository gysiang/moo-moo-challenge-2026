from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Submission:
    order_id: str
    ticket_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: float


@dataclass(frozen=True)
class ReplayState:
    submissions: tuple[Submission, ...]
    reconciliations: tuple[dict[str, Any], ...]
    kill_switch_engaged: bool
    halted: bool

    def canonical(self) -> str:
        return json.dumps(
            {
                "submissions": [
                    {
                        "order_id": s.order_id,
                        "ticket_id": s.ticket_id,
                        "symbol": s.symbol,
                        "side": s.side,
                        "quantity": s.quantity,
                        "limit_price": s.limit_price,
                    }
                    for s in self.submissions
                ],
                "reconciliations": list(self.reconciliations),
                "kill_switch_engaged": self.kill_switch_engaged,
                "halted": self.halted,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class EventLog:
    def __init__(self, path: str | Path | None = None, append: bool = False) -> None:
        self._path = Path(path) if path else None
        self.records: list[dict[str, Any]] = []
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if append and self._path.exists():
                self.records = read_events(self._path)
            else:
                self._path.write_text("", encoding="utf-8")
        self._next_seq = self.records[-1]["seq"] + 1 if self.records else 0

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"seq": self._next_seq, "event": event, **fields}
        self._next_seq += 1
        self.records.append(record)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record


def read_events(path: str | Path) -> list[dict[str, Any]]:
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def mandated_exits_in_run(path: str | Path, run_key: str) -> int:
    file = Path(path)
    if not run_key or not file.exists():
        return 0
    return sum(
        1
        for event in read_events(file)
        if event.get("event") == "ORDER_SUBMITTED"
        and event.get("run_key") == run_key
        and event.get("exit_reason")
    )


def replay(path: str | Path) -> ReplayState:
    submissions: list[Submission] = []
    reconciliations: list[dict[str, Any]] = []
    killed = False
    halted = False
    for record in read_events(path):
        event = record.get("event")
        if event == "ORDER_SUBMITTED":
            submissions.append(
                Submission(
                    order_id=record["order_id"],
                    ticket_id=record["ticket_id"],
                    symbol=record["symbol"],
                    side=record["side"],
                    quantity=record["quantity"],
                    limit_price=record["limit_price"],
                )
            )
        elif event == "RECONCILED":
            reconciliations.append(
                {
                    "order_id": record["order_id"],
                    "submitted": record["submitted"],
                    "filled": record["filled"],
                    "unfilled": record["unfilled"],
                    "status": record["status"],
                    "position_quantity": record["position_quantity"],
                }
            )
        elif event == "KILL_SWITCH":
            killed = True
        elif event == "HALTED":
            halted = True
    return ReplayState(
        submissions=tuple(submissions),
        reconciliations=tuple(reconciliations),
        kill_switch_engaged=killed,
        halted=halted,
    )
