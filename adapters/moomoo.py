from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from gate.models import Position
from guard.broker import BrokerAck, BrokerError, OrderRequest, OrderState, UnknownOutcome

CHALLENGE_ACCOUNT_ID = "5829804"
US_MARKET_ID = 100

TOOL_PLACE = "sim_trade_input_order"
TOOL_CANCEL = "sim_trade_cancel_order"
TOOL_ORDERS = "sim_trade_history_order_list"
TOOL_POSITIONS = "sim_trade_position_list"
TOOL_CASH = "sim_trade_cash_info"

TICKET_PREFIX = "qdr:"
_MAX_PAGES = 5
_PAGE_SIZE = 200


class ToolCaller(Protocol):
    def __call__(self, tool: str, arguments: dict) -> dict: ...


# Futu/moomoo ORDER_STATUS codes.
_STATUS_BY_CODE = {
    1: "ACKED",  # submitting
    2: "ACKED",  # submitted
    3: "PARTIAL",  # filled part
    4: "FILLED",
    5: "PARTIAL",  # cancelling part (still working)
    6: "PARTIAL_CANCELLED",
    7: "CANCELLED",
    8: "REJECTED",  # failed
    9: "REJECTED",  # disabled
    10: "CANCELLED",  # deleted
    11: "ACKED",  # waiting to submit
    12: "REJECTED",  # submit failed
    13: "UNKNOWN",  # timeout
}


def normalise_status(raw: object) -> str:
    if raw is None:
        return "UNKNOWN"
    if isinstance(raw, (int, float)):
        return _STATUS_BY_CODE.get(int(raw), "UNKNOWN")
    text = str(raw).strip().upper()
    if text.isdigit():
        return _STATUS_BY_CODE.get(int(text), "UNKNOWN")
    if "PART" in text and "CANCEL" in text:
        return "PARTIAL_CANCELLED"
    if "PART" in text and "FILL" in text:
        return "PARTIAL"
    if "CANCEL" in text or "DELET" in text or "DISABLE" in text:
        return "CANCELLED"
    if "FAIL" in text or "REJECT" in text:
        return "REJECTED"
    if "FILL" in text:
        return "FILLED"
    if "SUBMIT" in text or "WAIT" in text or "ACK" in text:
        return "ACKED"
    return "UNKNOWN"


def _num(value: object, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _int(value: object, default: int = 0) -> int:
    return int(_num(value, default))


def _side(raw: object) -> str:
    if isinstance(raw, (int, float)):
        return "BUY" if int(raw) == 1 else "SELL"
    text = str(raw).upper()
    if "BUY" in text or text == "1":
        return "BUY"
    return "SELL"


class MoomooBroker:
    """Broker Protocol implementation over moomoo sim-trade MCP tools.

    Every call is a single `sim_trade_*` invocation. Transport failures during
    submit are ambiguous and surface as UnknownOutcome so the Guard halts;
    explicit API rejections surface as BrokerError and are logged as rejected.
    """

    def __init__(
        self,
        call: ToolCaller,
        acc_id: str = CHALLENGE_ACCOUNT_ID,
        market: int = US_MARKET_ID,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._call = call
        self._acc_id = acc_id
        self._market = market
        self._clock = clock

    # -- Broker protocol -------------------------------------------------
    def submit(self, request: OrderRequest) -> BrokerAck:
        note = f"{TICKET_PREFIX}{request.ticket_id}"
        arguments = {
            "acc_id": self._acc_id,
            "market": self._market,
            "symbol": request.symbol,
            "order_type": 1,
            "order_side": 1 if request.side == "BUY" else 2,
            "qty": str(request.quantity),
            "price": f"{request.limit_price:.4f}",
            "text": note[:100],
        }
        try:
            response = self._call(TOOL_PLACE, arguments)
        except BrokerError:
            raise
        except Exception as exc:  # transport/ambiguous
            raise UnknownOutcome(f"place_order failed: {exc}") from exc

        data = self._unpack(response)
        order_id = data.get("order_id", data.get("order_id_ex"))
        if order_id in (None, ""):
            raise UnknownOutcome("place_order returned no order_id")
        return BrokerAck(str(order_id), "ACKED")

    def cancel(self, order_id: str) -> None:
        response = self._call(
            TOOL_CANCEL,
            {"acc_id": self._acc_id, "market": self._market, "order_id": order_id},
        )
        self._unpack(response)

    def order(self, order_id: str) -> OrderState:
        for state in self._orders():
            if state.order_id == str(order_id):
                return state
        raise BrokerError(f"order {order_id} not found in recent sim orders")

    def working_orders(self) -> list[OrderState]:
        return [state for state in self._orders() if state.is_working]

    def positions(self) -> dict[str, Position]:
        data = self._unpack(
            self._call(
                TOOL_POSITIONS,
                {"acc_id": self._acc_id, "market": self._market},
            )
        )
        result: dict[str, Position] = {}
        for row in data.get("positions") or []:
            symbol = row.get("code") or row.get("symbol")
            if not symbol:
                continue
            quantity = _int(row.get("qty"))
            average_cost = _num(
                row.get("cost_price", row.get("average_cost", row.get("avg_cost")))
            )
            market_value = _num(row.get("market_val", row.get("market_value")), quantity * average_cost)
            result[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                market_value=market_value,
                average_cost=average_cost,
            )
        return result

    # -- account snapshot ------------------------------------------------
    def account_snapshot(self) -> dict:
        cash = self._unpack(self._call(TOOL_CASH, {"acc_id": self._acc_id}))
        positions = self.positions()
        return {
            "cash": _num(cash.get("balance")),
            "equity": _num(cash.get("total_asset")),
            "max_buying_power": _num(cash.get("max_power_long")),
            "positions": {symbol: asdict(pos) for symbol, pos in positions.items()},
            "orders": [asdict(state) for state in self._orders()],
        }

    # -- internals -------------------------------------------------------
    def _unpack(self, response: object) -> dict:
        if not isinstance(response, dict):
            raise BrokerError("malformed response from moomoo tool")
        code = response.get("ret_code", response.get("retCode", 0))
        if code not in (0, "0", None):
            message = response.get("ret_msg") or response.get("retMsg") or ""
            raise BrokerError(f"ret_code {code}: {message}")
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    def _orders(self) -> list[OrderState]:
        end = self._clock()
        arguments: dict = {
            "acc_id": self._acc_id,
            "market": self._market,
            "page_size": _PAGE_SIZE,
            "time_begin": int((end - timedelta(days=2)).timestamp() * 1_000_000),
            "time_end": int((end + timedelta(hours=1)).timestamp() * 1_000_000),
        }
        rows: list[dict] = []
        for _ in range(_MAX_PAGES):
            data = self._unpack(self._call(TOOL_ORDERS, arguments))
            rows.extend(data.get("orders") or [])
            pagination = {"has_more": False}
            if isinstance(data.get("pagination"), dict):
                pagination = data["pagination"]
            next_key: Optional[str] = pagination.get("next_key")
            if not pagination.get("has_more") or not next_key:
                break
            arguments = {**arguments, "next_key": next_key}
        return [self._to_state(row) for row in rows]

    def _to_state(self, row: dict) -> OrderState:
        note = str(row.get("remark") or row.get("text") or "")
        ticket_id = note[len(TICKET_PREFIX):] if note.startswith(TICKET_PREFIX) else ""
        return OrderState(
            order_id=str(row.get("order_id", row.get("order_id_ex", ""))),
            ticket_id=ticket_id,
            symbol=str(row.get("code") or row.get("symbol") or ""),
            side=_side(row.get("trd_side", row.get("order_side"))),
            quantity=_int(row.get("qty", row.get("order_qty"))),
            filled_quantity=_int(row.get("dealt_qty", row.get("filled_qty"))),
            avg_fill_price=_num(row.get("dealt_avg_price", row.get("avg_fill_price"))),
            status=normalise_status(row.get("order_status", row.get("status"))),
        )
