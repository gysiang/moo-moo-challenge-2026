from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gate.models import Position


class UnknownOutcome(Exception):
    """Broker did not confirm or reject the request; state must be reconciled."""


class BrokerError(Exception):
    pass


@dataclass(frozen=True)
class OrderRequest:
    ticket_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float


@dataclass(frozen=True)
class BrokerAck:
    order_id: str
    status: str


@dataclass(frozen=True)
class OrderState:
    order_id: str
    ticket_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    avg_fill_price: float
    status: str

    @property
    def unfilled_quantity(self) -> int:
        return max(self.quantity - self.filled_quantity, 0)

    @property
    def is_working(self) -> bool:
        return self.status in ("ACKED", "PARTIAL") and self.unfilled_quantity > 0


class Broker(Protocol):
    def submit(self, request: OrderRequest) -> BrokerAck: ...

    def cancel(self, order_id: str) -> None: ...

    def order(self, order_id: str) -> OrderState: ...

    def working_orders(self) -> list[OrderState]: ...

    def positions(self) -> dict[str, Position]: ...
