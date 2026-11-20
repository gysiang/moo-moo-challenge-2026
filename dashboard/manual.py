from __future__ import annotations

import json
import math
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adapters.moomoo import normalise_status
from dashboard.cloud import CloudClient, CloudError
from dashboard import state
from gate import Evidence, load_limits
from gate.models import AccountState, Position, WorkingOrder
from scout.cycle import Scout
from scout.marketdata import write_snapshot
from scout.settings import load_strategy
from scout.slots import eastern, market_session
from scout.watchlist import load_watchlist

IDENTITY = "idn_4a6682c9f973d0ba24f3ffa2"
SCOUT_TEMPLATE = "tmpl_834e2027a979a145b432ff03"
GUARD_TEMPLATE = "tmpl_a804c126b3b5afe0dd349273"
ACTIVE = {"creating", "sending", "running", "unknown"}


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed.astimezone(timezone.utc)


def number(value) -> float:
    result = float(value)
    if isinstance(value, bool) or not math.isfinite(result):
        raise ValueError("invalid broker number")
    return result


def quantity(value) -> int:
    result = number(value)
    if result < 0 or result != int(result):
        raise ValueError("invalid broker quantity")
    return int(result)


def data(response: dict) -> dict:
    if response.get("ret_code") not in (0, "0") or not isinstance(response.get("data"), dict):
        raise ValueError("broker response failed or is incomplete")
    return response["data"]


def symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:US\.)?[A-Z0-9][A-Z0-9.\-]*", value):
        raise ValueError("invalid US symbol")
    return value if value.startswith("US.") else "US." + value


def normalize_account(payload: dict, previous: dict) -> dict:
    stamp = moment(payload["as_of"])
    cash = data(payload["cash"])
    positions = {}
    for row in data(payload["positions"])["positions"]:
        if row.get("market") != 100 or row.get("pstn_type") != 0:
            raise ValueError("unexpected position market or direction")
        key = symbol(row["symbol"])
        if key in positions:
            raise ValueError("duplicate broker position")
        positions[key] = {"quantity": quantity(row["qty"]), "market_value": number(row["mv"]),
                          "average_cost": number(row["cost_price"])}
    pages = payload["order_pages"]
    if not isinstance(pages, list) or not pages or len(pages) > 100:
        raise ValueError("order history pages missing")
    orders = []
    ids = set()
    for index, page in enumerate(pages):
        pagination = page.get("pagination", {})
        if pagination.get("has_more") is not (index < len(pages) - 1):
            raise ValueError("incomplete order history pagination")
        for row in data(page)["orders"]:
            order_id = str(row["order_id"])
            if order_id in ids:
                raise ValueError("duplicate order history page")
            ids.add(order_id)
            if row["side"] not in (1, 2, "1", "2"):
                raise ValueError("invalid order side")
            qty, filled = quantity(row["qty"]), quantity(row["cum_qty"])
            if filled > qty:
                raise ValueError("filled quantity exceeds order quantity")
            status = normalise_status(row["status"])
            created = datetime.fromtimestamp(int(row["create_time"]) / 1_000_000, timezone.utc)
            orders.append({"order_id": order_id, "symbol": symbol(row["symbol"]),
                           "side": "BUY" if str(row["side"]) == "1" else "SELL",
                           "quantity": qty, "filled_quantity": filled, "limit_price": number(row["price"]),
                           "average_fill_price": number(row["avg_fill_price"]) if row.get("avg_fill_price") else None,
                           "status": status, "created_at": created.isoformat(),
                           "ticket_id": str(row.get("text", "")).removeprefix("qdr:") or None})
    working = [o for o in orders if o["status"] in ("ACKED", "PARTIAL", "UNKNOWN")]
    equity = number(cash["total_asset"])
    if equity <= 0:
        raise ValueError("invalid account equity")
    account = {"account_id": "5829804", "as_of": stamp.isoformat(), "cash": number(cash["balance"]),
               "equity": equity, "positions": positions, "working_orders": working, "orders": orders,
               "orders_complete": True, "baseline_verified": False,
               "executed_orders_today": sum(eastern(moment(o["created_at"])).date() == eastern(stamp).date()
                                             for o in orders)}
    if previous.get("day_open_equity") and previous.get("as_of"):
        if eastern(moment(previous["as_of"])).date() == eastern(stamp).date() and previous.get("baseline_verified", True):
            baseline = number(previous["day_open_equity"])
            if baseline > 0:
                account.update(day_open_equity=baseline, baseline_verified=True)
    return account


def account_state(account: dict, now: datetime) -> AccountState:
    baseline = account.get("day_open_equity")
    return AccountState(now=now, cash=account["cash"], equity=account["equity"], account_id="5829804",
                        positions={s: Position(symbol=s, **p) for s, p in account["positions"].items()},
                        working_orders=tuple(WorkingOrder(o["symbol"], o["side"]) for o in account["working_orders"]),
                        executed_orders_today=account["executed_orders_today"],
                        daily_pnl_pct=account["equity"] / baseline - 1 if baseline else 0.0)


class ManualActions:
    def __init__(self, client: CloudClient, runtime: Path, config: Path, lock=None):
        self.client, self.runtime, self.config = client, Path(runtime), Path(config)
        self.lock = lock or threading.RLock()
        self.path = self.runtime / "dashboard-action.json"

    def status(self) -> dict:
        stored = state.read_json(self.path)
        if not isinstance(stored, dict) or "status" not in stored:
            stored = {"status": "unknown" if self.path.exists() else "idle"}
        return {"configured": True, **stored}

    def start(self, mode: str) -> dict:
        if mode not in ("scout", "refresh"):
            raise ValueError("unknown dashboard action")
        with self.lock:
            current = self.status()
            if current["status"] in ACTIVE:
                return {"ok": False, "error": "An action is already active or uncertain; inspect its session first.", **current}
            now = datetime.now(timezone.utc)
            request = {"request_id": uuid.uuid4().hex, "mode": mode, "status": "creating", "started_at": now.isoformat()}
            atomic_json(self.path, request)
            try:
                session = self.client.create_forward_session(IDENTITY, SCOUT_TEMPLATE,
                                                            f"Dashboard {mode} {request['request_id']}")
                request.update(session_id=session["id"], status="sending")
                atomic_json(self.path, request)
                self.client.send_forward_message(session["id"], json.dumps({
                    "kind": "dashboard-request", "version": 1, "mode": mode,
                    "request_id": request["request_id"], "account_id": "5829804", "market": 100,
                    "requested_at": request["started_at"],
                }))
                request["status"] = "running"
            except CloudError as exc:
                request.update(status="unknown" if not exc.status or exc.status >= 500 else "failed",
                               error="Cloud action could not be confirmed; no automatic retry. " + exc.message)
            atomic_json(self.path, request)
            return {"ok": request["status"] == "running", **request}

    def poll(self) -> dict:
        with self.lock:
            request = self.status()
            if request["status"] not in ACTIVE or not request.get("session_id"):
                return request
            try:
                name = f"dashboard-{request['request_id']}.json"
                files = self.client.files(scope_id=request["session_id"], name=name, limit=100)
                matches = [f for f in files if f.get("filename") == name and
                           (f.get("scope") or {}).get("id") == request["session_id"]]
                if len(matches) > 1:
                    raise ValueError("multiple results for one request")
                if matches:
                    blob = self.client.download_file(matches[0]["id"])
                    if len(blob) > 2_000_000:
                        raise ValueError("dashboard result too large")
                    payload = json.loads(blob)
                    self.apply(request, payload)
                    request.update(status="completed", completed_at=datetime.now(timezone.utc).isoformat())
                    request.pop("error", None)
                else:
                    session = self.client.forward_session(request["session_id"])
                    if session.get("status") in ("terminated", "failed", "cancelled"):
                        request.update(status="failed", error="Cloud session ended without delivering its result.")
                    elif datetime.now(timezone.utc) - moment(request["started_at"]) > timedelta(minutes=30):
                        request.update(status="unknown", error="No result after 30 minutes; inspect the cloud session. Do not restart blindly.")
            except CloudError as exc:
                request["error"] = exc.message
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
                request.update(status="failed", error="Result rejected: incomplete, stale, conflicting or invalid evidence. Previous snapshot retained.")
            atomic_json(self.path, request)
            return request

    def apply(self, request: dict, payload: dict) -> None:
        if payload.get("version") != 1 or payload.get("request_id") != request["request_id"] or payload.get("mode") != request["mode"]:
            raise ValueError("result request mismatch")
        if payload.get("account_id") != "5829804" or payload.get("market") != 100:
            raise ValueError("result account mismatch")
        now = datetime.now(timezone.utc)
        stamp = moment(payload["as_of"])
        if stamp < moment(request["started_at"]) - timedelta(seconds=5) or not -5 <= (now - stamp).total_seconds() <= 1800:
            raise ValueError("stale result")
        committed_before = state.read_json(self.runtime / "dashboard-snapshot.json") or {}
        if committed_before.get("request_id") == request["request_id"]:
            return
        previous = committed_before.get("account") or state.read_json(self.runtime / "account-5829804.json") or {}
        if previous.get("as_of") and moment(previous["as_of"]) > stamp:
            raise ValueError("older account snapshot")
        account = normalize_account(payload, previous)
        metrics = None
        cycle = None
        if request["mode"] == "scout":
            raw = payload["market_raw"]
            raw_stamp = moment(raw["as_of"])
            if not -5 <= (now - raw_stamp).total_seconds() <= 1800 or raw_stamp < moment(request["started_at"]) - timedelta(seconds=5):
                raise ValueError("stale market data")
            watchlist = load_watchlist(self.config.parent / "scout/watchlist.json")
            allowed = {row.symbol for row in watchlist.core} | set(watchlist.satellites)
            if not set(raw["symbols"]).issubset(allowed):
                raise ValueError("symbol outside fixed watchlist")
            for row in raw["symbols"].values():
                collected = moment(row.get("as_of", raw["as_of"]))
                if not -5 <= (now - collected).total_seconds() <= 1800 or collected < moment(request["started_at"]) - timedelta(seconds=5):
                    raise ValueError("stale symbol data")
                if row["instrument_type"] not in ("stock", "etf") or number(row["market_cap"]) < 0:
                    raise ValueError("invalid instrument facts")
                if len(row["closes"]) > 1000 or len(row["highs"]) > 1000 or any(number(v) <= 0 for v in row["closes"] + row["highs"]):
                    raise ValueError("invalid price history")
            settings = load_strategy(self.config)
            staging = self.runtime / f"metrics-{request['request_id']}.tmp"
            try:
                snapshot = write_snapshot(raw, staging, settings.trend_fast_ema, settings.trend_slow_ema)
                metrics = json.loads(staging.read_text(encoding="utf-8"))
            finally:
                staging.unlink(missing_ok=True)
            news = {s: tuple(Evidence(e["id"], "news", moment(e["as_of"]), e["source"]) for e in items)
                    for s, items in payload["news"].items()}
            market = market_session(now)
            entry = market["state"] == "OPEN" and moment(market["closes_at"]) - now > timedelta(minutes=30)
            scout = Scout(load_limits(self.config), settings, watchlist)
            cycle = scout.run(f"{eastern(now):%H:%M}", now, snapshot, account_state(account, now),
                              news=news, allow_entries=entry).to_dict()
            if market["state"] != "OPEN":
                cycle["records"] = []
                cycle["skipped"].append({"symbol": "*", "reason": "MARKET_CLOSED"})
            if not account["baseline_verified"]:
                for row in cycle["records"]:
                    row["status"] = "REJECTED"
                    row["gate"]["passed"] = False
                    row["gate"]["codes"].append("R8_DAILY_BASELINE_UNVERIFIED")
            old_ids = {r["ticket_id"] for c in state.list_cycles(self.runtime) for r in c["payload"]["records"]}
            if old_ids.intersection(r["ticket_id"] for r in cycle["records"]):
                raise ValueError("ticket minute collision; keep original tickets")
            cycle["request_id"] = request["request_id"]
        # The bundle is the commit point; readers never combine snapshots from different runs.
        committed = {"request_id": request["request_id"], "account": account,
                     "scout_request_id": request["request_id"] if cycle is not None else committed_before.get("scout_request_id"),
                     "metrics": metrics or (state.read_json(self.runtime / "dashboard-snapshot.json") or {}).get("metrics")
                     or state.read_json(self.runtime / "metrics.json") or {}}
        if cycle is not None:
            name = f"cycle-{eastern(now):%Y%m%d-%H%M}-{request['request_id']}.json"
            atomic_json(self.runtime / name, cycle)
        atomic_json(self.runtime / "dashboard-snapshot.json", committed)
        atomic_json(self.runtime / "account-5829804.json", account)
        if metrics is not None:
            atomic_json(self.runtime / "metrics.json", metrics)
            atomic_json(self.runtime / "news.json", payload["news"])
