from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from gate.models import AccountState, Evidence, Instrument, Position, Proposal, WorkingOrder
from scout.slots import eastern

ACCOUNT_ID = "5829804"
MAX_BYTES = 2_000_000
BUNDLE_NAME = re.compile(r"^coordinator-\d{8}-\d{4}(?:-rerun)?\.json$")
TICKET_ID = re.compile(r"^P-\d{8}-\d{4}-[A-Z0-9.]+-\d{2}$")


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO string")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return moment.astimezone(timezone.utc)


def number(value, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a JSON number")
    if value < 0 or value > 1e18 or not math.isfinite(value) or (positive and value == 0):
        raise ValueError("expected a finite nonnegative number")
    return value


def integer(value) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("expected a nonnegative integer")
    return value


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(payload: bytes) -> dict:
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError("bundle is empty or exceeds size limit")
    def bad_constant(_):
        raise ValueError("non-finite JSON number")
    result = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
                        parse_constant=bad_constant)
    if not isinstance(result, dict):
        raise ValueError("bundle must be a JSON object")
    return result


def account_state(account: dict, now: datetime, baseline: float) -> AccountState:
    if account["account_id"] != ACCOUNT_ID:
        raise ValueError("wrong paper account")
    timestamp(account["as_of"])
    equity = number(account["equity"], positive=True)
    cash = number(account["cash"])
    orders = integer(account["executed_orders_today"])
    positions = {}
    for symbol, row in account["positions"].items():
        if not isinstance(symbol, str) or not symbol.startswith("US."):
            raise ValueError("non-US position")
        qty = integer(row["quantity"])
        if qty == 0:
            raise ValueError("position quantity must be positive")
        positions[symbol] = Position(symbol, qty, number(row["market_value"]),
                                     number(row["average_cost"], positive=True))
    working = []
    for row in account["working_orders"]:
        if row["side"] not in ("BUY", "SELL") or not row["symbol"].startswith("US."):
            raise ValueError("invalid working order")
        working.append(WorkingOrder(row["symbol"], row["side"]))
    return AccountState(now=now, cash=cash, equity=equity, positions=positions,
                        working_orders=tuple(working), executed_orders_today=orders,
                        daily_pnl_pct=equity / baseline - 1, account_id=ACCOUNT_ID)


def proposal(record: dict) -> Proposal:
    if not TICKET_ID.fullmatch(record["ticket_id"]):
        raise ValueError("invalid ticket id")
    qty = integer(record["quantity"])
    if qty == 0:
        raise ValueError("quantity must be positive")
    items = []
    for item in record["evidence"]:
        if not all(isinstance(item[k], str) for k in ("id", "kind", "source")):
            raise ValueError("invalid evidence")
        items.append(Evidence(item["id"], item["kind"], timestamp(item["as_of"]), item["source"]))
    result = Proposal(ticket_id=record["ticket_id"], created_at=timestamp(record["created_at"]),
                      symbol=record["symbol"], side=record["side"], quantity=qty,
                      order_type=record["order_type"], limit_price=number(record["limit_price"], positive=True),
                      est_cost=number(record["est_cost"], positive=True), starter_tranche=1,
                      evidence=tuple(items), exit_reason=record.get("exit_reason", ""))
    if result.canonical_hash() != record["proposal_hash"]:
        raise ValueError("proposal hash mismatch")
    return result


def instrument(symbol: str, metrics: dict) -> Instrument:
    row = metrics["symbols"][symbol]
    for key in ("us_listed", "tradable"):
        if key in row and type(row[key]) is not bool:
            raise ValueError("invalid instrument flag")
    return Instrument(symbol, row["instrument_type"], row.get("us_listed", True),
                      number(row["last_price"], positive=True), number(row["market_cap"]),
                      row.get("tradable", True))


def make_bundle(cycle: dict, account: dict, metrics: dict) -> dict:
    clean_account = {key: account[key] for key in
                     ("account_id", "as_of", "cash", "equity", "executed_orders_today")}
    clean_account["positions"] = {
        symbol: {key: row[key] for key in ("quantity", "market_value", "average_cost")}
        for symbol, row in account["positions"].items()
    }
    clean_account["working_orders"] = [
        {key: row[key] for key in ("symbol", "side")} for row in account["working_orders"]
    ]
    clean_cycle = {key: cycle[key] for key in
                   ("slot", "generated_at", "trading_day", "entries_allowed")}
    clean_cycle["records"] = []
    for row in cycle["records"]:
        record = {key: row[key] for key in ("sleeve", "status", "ticket_id", "proposal_hash",
                  "symbol", "side", "quantity", "order_type", "limit_price", "est_cost", "created_at")}
        record["exit_reason"] = row.get("exit_reason", "")
        record["evidence"] = [{key: item[key] for key in ("id", "kind", "as_of", "source")}
                              for item in row["evidence"]]
        clean_cycle["records"].append(record)
    clean_metrics = {"as_of": metrics["as_of"], "symbols": {
        symbol: {key: row[key] for key in ("instrument_type", "last_price", "market_cap",
                                          "us_listed", "tradable") if key in row}
        for symbol, row in metrics["symbols"].items()
    }}
    account_state(clean_account, timestamp(clean_account["as_of"]), number(account["equity"], positive=True))
    for record in clean_cycle["records"]:
        proposal(record)
        instrument(record["symbol"], clean_metrics)
    timestamp(clean_cycle["generated_at"])
    timestamp(clean_metrics["as_of"])
    return {"kind": "coordinator-bundle", "version": 1, "account_id": ACCOUNT_ID,
            "cycle": clean_cycle, "account": clean_account, "metrics": clean_metrics}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a read-only coordinator bundle; never refresh timestamps")
    for name in ("cycle", "account", "metrics", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    result = make_bundle(*(decode(Path(getattr(args, name)).read_bytes())
                           for name in ("cycle", "account", "metrics")))
    path = Path(args.out)
    if not BUNDLE_NAME.fullmatch(path.name):
        parser.error("output must be coordinator-YYYYMMDD-HHMM.json (or -rerun.json)")
    generated = eastern(timestamp(result["cycle"]["generated_at"]))
    stem = f"coordinator-{generated:%Y%m%d}-{result['cycle']['slot'].replace(':', '')}"
    if path.name not in (stem + ".json", stem + "-rerun.json"):
        parser.error("output name must match the cycle date and slot")
    payload = canonical(result).encode("utf-8")
    if len(payload) > MAX_BYTES:
        parser.error("bundle exceeds size limit")
    with path.open("xb") as handle:
        handle.write(payload)
    print(f"DRY_RUN bundle written: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
