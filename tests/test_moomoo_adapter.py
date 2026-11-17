from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from adapters.moomoo import (
    CHALLENGE_ACCOUNT_ID,
    MoomooBroker,
    normalise_status,
)
from guard.broker import BrokerError, OrderRequest, UnknownOutcome

NOW = datetime(2026, 11, 17, 14, 45, tzinfo=timezone.utc)


class FakeCaller:
    def __init__(self, responses: dict[str, list] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, dict(arguments)))
        queue = self.responses.get(tool)
        if not queue:
            raise AssertionError(f"unexpected tool call {tool}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestStatusMapping(unittest.TestCase):
    def test_futu_codes(self) -> None:
        self.assertEqual(normalise_status(2), "ACKED")
        self.assertEqual(normalise_status(3), "PARTIAL")
        self.assertEqual(normalise_status(4), "FILLED")
        self.assertEqual(normalise_status(6), "PARTIAL_CANCELLED")
        self.assertEqual(normalise_status(7), "CANCELLED")
        self.assertEqual(normalise_status(8), "REJECTED")
        self.assertEqual(normalise_status(13), "UNKNOWN")

    def test_string_statuses(self) -> None:
        self.assertEqual(normalise_status("FILLED_ALL"), "FILLED")
        self.assertEqual(normalise_status("FILLED_PART"), "PARTIAL")
        self.assertEqual(normalise_status("CANCELLED_PART"), "PARTIAL_CANCELLED")
        self.assertEqual(normalise_status("SUBMITTED"), "ACKED")
        self.assertEqual(normalise_status("SUBMIT_FAILED"), "REJECTED")
        self.assertEqual(normalise_status("SOMETHING_NEW"), "UNKNOWN")

    def test_numeric_string_and_none(self) -> None:
        self.assertEqual(normalise_status("4"), "FILLED")
        self.assertEqual(normalise_status(None), "UNKNOWN")


class TestSubmit(unittest.TestCase):
    def make(self, responses: dict[str, list]) -> tuple[MoomooBroker, FakeCaller]:
        caller = FakeCaller(responses)
        return MoomooBroker(caller, clock=lambda: NOW), caller

    def test_place_order_payload_and_ack(self) -> None:
        broker, caller = self.make(
            {"sim_trade_input_order": [{"ret_code": 0, "data": {"order_id": "9001"}}]}
        )
        ack = broker.submit(
            OrderRequest("P-1", "US.SPY", "BUY", 14, "LIMIT", 739.95)
        )

        self.assertEqual(ack.order_id, "9001")
        self.assertEqual(ack.status, "ACKED")
        tool, args = caller.calls[0]
        self.assertEqual(tool, "sim_trade_input_order")
        self.assertEqual(args["acc_id"], CHALLENGE_ACCOUNT_ID)
        self.assertEqual(args["market"], 100)
        self.assertEqual(args["symbol"], "US.SPY")
        self.assertEqual(args["order_type"], 1)
        self.assertEqual(args["order_side"], 1)
        self.assertEqual(args["qty"], "14")
        self.assertEqual(args["price"], "739.9500")
        self.assertEqual(args["text"], "qdr:P-1")

    def test_sell_side_code(self) -> None:
        broker, caller = self.make(
            {"sim_trade_input_order": [{"ret_code": 0, "data": {"order_id": "9002"}}]}
        )
        broker.submit(OrderRequest("P-2", "US.SPY", "SELL", 5, "LIMIT", 740.0))
        self.assertEqual(caller.calls[0][1]["order_side"], 2)

    def test_transport_error_is_unknown(self) -> None:
        broker, _ = self.make({"sim_trade_input_order": [TimeoutError("socket")]})
        with self.assertRaises(UnknownOutcome):
            broker.submit(OrderRequest("P-1", "US.SPY", "BUY", 1, "LIMIT", 740.0))

    def test_api_rejection_is_definite(self) -> None:
        broker, _ = self.make(
            {
                "sim_trade_input_order": [
                    {"ret_code": -1, "ret_msg": "insufficient buying power"}
                ]
            }
        )
        with self.assertRaises(BrokerError):
            broker.submit(OrderRequest("P-1", "US.SPY", "BUY", 1, "LIMIT", 740.0))

    def test_missing_order_id_is_unknown(self) -> None:
        broker, _ = self.make({"sim_trade_input_order": [{"ret_code": 0, "data": {}}]})
        with self.assertRaises(UnknownOutcome):
            broker.submit(OrderRequest("P-1", "US.SPY", "BUY", 1, "LIMIT", 740.0))


class TestReads(unittest.TestCase):
    def make(self, responses: dict[str, list]) -> tuple[MoomooBroker, FakeCaller]:
        caller = FakeCaller(responses)
        return MoomooBroker(caller, clock=lambda: NOW), caller

    def order_page(self, orders: list[dict], has_more: bool = False, next_key: str = "0") -> dict:
        return {
            "ret_code": 0,
            "data": {
                "orders": orders,
                "pagination": {"has_more": has_more, "next_key": next_key},
            },
        }

    def test_order_state_mapping(self) -> None:
        broker, _ = self.make(
            {
                "sim_trade_history_order_list": [
                    self.order_page(
                        [
                            {
                                "order_id": 9001,
                                "code": "US.SPY",
                                "trd_side": 1,
                                "order_status": 3,
                                "qty": "100",
                                "dealt_qty": "60",
                                "dealt_avg_price": 739.9,
                                "remark": "qdr:P-1",
                            }
                        ]
                    )
                ]
            }
        )
        state = broker.order("9001")
        self.assertEqual(state.ticket_id, "P-1")
        self.assertEqual(state.filled_quantity, 60)
        self.assertEqual(state.unfilled_quantity, 40)
        self.assertEqual(state.status, "PARTIAL")
        self.assertTrue(state.is_working)

    def test_partial_cancelled_is_not_working(self) -> None:
        broker, _ = self.make(
            {
                "sim_trade_history_order_list": [
                    self.order_page(
                        [
                            {
                                "order_id": "1",
                                "code": "US.SPY",
                                "trd_side": "BUY",
                                "order_status": "CANCELLED_PART",
                                "qty": "100",
                                "dealt_qty": "40",
                            }
                        ]
                    )
                ]
            }
        )
        states = broker.working_orders()
        self.assertEqual(states, [])

    def test_pagination_follows_next_key(self) -> None:
        broker, caller = self.make(
            {
                "sim_trade_history_order_list": [
                    self.order_page([{"order_id": "1", "order_status": 2, "qty": "1"}], True, "abc"),
                    self.order_page([{"order_id": "2", "order_status": 2, "qty": "1"}]),
                ]
            }
        )
        working = broker.working_orders()
        self.assertEqual([s.order_id for s in working], ["1", "2"])
        self.assertEqual(caller.calls[1][1]["next_key"], "abc")
        self.assertIn("time_begin", caller.calls[0][1])
        self.assertGreater(caller.calls[0][1]["time_end"], caller.calls[0][1]["time_begin"])

    def test_positions_mapping(self) -> None:
        broker, _ = self.make(
            {
                "sim_trade_position_list": [
                    {
                        "ret_code": 0,
                        "data": {
                            "positions": [
                                {
                                    "code": "US.SPY",
                                    "qty": "10",
                                    "cost_price": "730.5",
                                    "market_val": "7399.5",
                                }
                            ]
                        },
                    }
                ]
            }
        )
        positions = broker.positions()
        self.assertEqual(positions["US.SPY"].quantity, 10)
        self.assertEqual(positions["US.SPY"].average_cost, 730.5)
        self.assertEqual(positions["US.SPY"].market_value, 7399.5)

    def test_account_snapshot(self) -> None:
        broker, _ = self.make(
            {
                "sim_trade_cash_info": [
                    {
                        "ret_code": 0,
                        "data": {"balance": "100000", "total_asset": "100500", "max_power_long": "200000"},
                    }
                ],
                "sim_trade_position_list": [{"ret_code": 0, "data": {"positions": []}}],
                "sim_trade_history_order_list": [self.order_page([])],
            }
        )
        snapshot = broker.account_snapshot()
        self.assertEqual(snapshot["cash"], 100000.0)
        self.assertEqual(snapshot["equity"], 100500.0)
        self.assertEqual(snapshot["positions"], {})
        self.assertEqual(snapshot["orders"], [])

    def test_cancel_rejection_raises(self) -> None:
        broker, caller = self.make(
            {"sim_trade_cancel_order": [{"ret_code": -1, "ret_msg": "too late"}]}
        )
        with self.assertRaises(BrokerError):
            broker.cancel("9001")
        self.assertEqual(caller.calls[0][1]["order_id"], "9001")


if __name__ == "__main__":
    unittest.main()
