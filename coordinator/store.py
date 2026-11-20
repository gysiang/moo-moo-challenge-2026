from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from gate import load_limits
from gate.models import Position, WorkingOrder
from gate.rules import estimate_fees, evaluate
from scout.slots import allows_entries, due_slot, eastern, market_session
from scout.watchlist import load_watchlist
from coordinator.bundle import (ACCOUNT_ID, account_state, canonical, decode, digest,
                                instrument, number, proposal, timestamp)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
MICRO = Decimal(1_000_000)
MAX_BUYS = 2
BUDGET_SHARE = Decimal("0.10")
EXECUTION_BLOCKERS = ["TRADING_DISABLED", "GUARD_HALTED"]


def money(value, rounding=ROUND_CEILING) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000000"):
        raise ValueError("invalid monetary amount")
    return int((amount * MICRO).to_integral_value(rounding=rounding))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("unsupported coordinator database")
            with connection:
                yield connection
        finally:
            connection.close()

    @classmethod
    def initialize(cls, path, *, pilot_id: str, starts_at: datetime, scout_agent_id: str):
        starts_at = timestamp(starts_at.isoformat())
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", pilot_id):
            raise ValueError("invalid pilot id")
        if not re.fullmatch(r"agent_[A-Za-z0-9]+", scout_agent_id):
            raise ValueError("invalid Scout agent id")
        store = cls(path)
        fd = os.open(store.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(store.path)) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript('''
                    CREATE TABLE control (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        mode TEXT NOT NULL CHECK(mode='DRY_RUN'),
                        trading_enabled INTEGER NOT NULL CHECK(trading_enabled=0),
                        guard_halted INTEGER NOT NULL CHECK(guard_halted=1),
                        pilot_id TEXT NOT NULL, starts_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                        scout_agent_id TEXT NOT NULL, revoked_at TEXT
                    );
                    CREATE TABLE baselines (
                        trading_day TEXT PRIMARY KEY, equity_micro INTEGER NOT NULL CHECK(equity_micro>0),
                        observed_at TEXT NOT NULL, source TEXT NOT NULL
                    );
                    CREATE TABLE reservations (
                        ticket_id TEXT PRIMARY KEY, proposal_hash TEXT NOT NULL,
                        record_hash TEXT NOT NULL, trading_day TEXT NOT NULL,
                        symbol TEXT NOT NULL UNIQUE, quantity INTEGER NOT NULL,
                        notional_micro INTEGER NOT NULL, fees_micro INTEGER NOT NULL,
                        session_id TEXT NOT NULL, file_id TEXT NOT NULL, result TEXT NOT NULL
                    );
                    CREATE TABLE tickets (
                        ticket_id TEXT PRIMARY KEY, record_hash TEXT NOT NULL, result TEXT NOT NULL
                    );
                    CREATE TABLE artifacts (
                        file_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, result TEXT NOT NULL
                    );
                    CREATE TABLE audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                        event TEXT NOT NULL, detail TEXT NOT NULL
                    );
                ''')
                conn.execute("INSERT INTO control VALUES(1,'DRY_RUN',0,1,?,?,?,?,NULL)",
                             (pilot_id, starts_at.isoformat(), (starts_at + timedelta(hours=24)).isoformat(),
                              scout_agent_id))
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                cls._audit(conn, starts_at, "DRY_RUN_INITIALIZED", {"pilot_id": pilot_id})
        except Exception:
            store.path.unlink(missing_ok=True)
            raise
        return store

    @staticmethod
    def _audit(conn, now, event, detail):
        conn.execute("INSERT INTO audit(at,event,detail) VALUES(?,?,?)",
                     (now.isoformat(), event, canonical(detail)))

    def add_baseline(self, trading_day: str, equity, observed_at: datetime, source: str):
        day = date.fromisoformat(trading_day)
        observed_at = timestamp(observed_at.isoformat())
        if eastern(observed_at).date() != day:
            raise ValueError("baseline observation must be on its ET trading day")
        if not isinstance(source, str) or not source.strip() or len(source) > 200:
            raise ValueError("baseline requires a short provenance reference")
        amount = money(number(equity, positive=True), ROUND_FLOOR)
        with self.transaction() as conn:
            existing = conn.execute("SELECT * FROM baselines WHERE trading_day=?", (trading_day,)).fetchone()
            values = (trading_day, amount, observed_at.isoformat(), source)
            if existing:
                if tuple(existing) != values:
                    raise ValueError("daily baseline is immutable")
                return
            conn.execute("INSERT INTO baselines VALUES(?,?,?,?)", values)
            self._audit(conn, observed_at, "BASELINE_PINNED", {"trading_day": trading_day})

    def revoke(self, now: datetime):
        now = timestamp(now.isoformat())
        with self.transaction() as conn:
            if conn.execute("SELECT revoked_at FROM control").fetchone()[0] is None:
                conn.execute("UPDATE control SET revoked_at=?", (now.isoformat(),))
                self._audit(conn, now, "DRY_RUN_REVOKED", {})

    def status(self) -> dict:
        with self.transaction() as conn:
            row = dict(conn.execute("SELECT * FROM control").fetchone())
            row.pop("id")
            row["trading_enabled"] = False
            row["guard_halted"] = True
            row["max_buys_per_et_day"] = MAX_BUYS
            row["daily_buy_fraction"] = str(BUDGET_SHARE)
            row["reservations"] = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            row["pending_notional"] = conn.execute(
                "SELECT COALESCE(SUM(notional_micro),0) FROM reservations").fetchone()[0] / 1_000_000
            row["baseline_days"] = [r[0] for r in conn.execute("SELECT trading_day FROM baselines ORDER BY trading_day")]
            row["artifacts"] = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            row["audit_events"] = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            return row

    def artifact_seen(self, file_id: str) -> bool:
        with self.transaction() as conn:
            return conn.execute("SELECT 1 FROM artifacts WHERE file_id=?", (file_id,)).fetchone() is not None

    def record_poll(self, now: datetime, summary: dict):
        with self.transaction() as conn:
            self._audit(conn, now, "POLL", summary)

    def process(self, payload: bytes, *, session_id: str, file_id: str,
                now: datetime, limits=None, watchlist=None) -> dict:
        now = timestamp(now.isoformat())
        if not re.fullmatch(r"sess_[A-Za-z0-9]+", session_id):
            raise ValueError("invalid source session")
        if not re.fullmatch(r"file_[A-Za-z0-9]+", file_id):
            raise ValueError("invalid source file")
        bundle = decode(payload)
        if set(bundle) != {"kind", "version", "account_id", "cycle", "account", "metrics"}:
            raise ValueError("unexpected bundle fields")
        if bundle["kind"] != "coordinator-bundle" or type(bundle["version"]) is not int or bundle["version"] != 1:
            raise ValueError("unsupported bundle")
        if bundle["account_id"] != ACCOUNT_ID or bundle["account"]["account_id"] != ACCOUNT_ID:
            raise ValueError("wrong paper account")
        cycle, account, metrics = (bundle[k] for k in ("cycle", "account", "metrics"))
        generated = timestamp(cycle["generated_at"])
        account_at, metrics_at = timestamp(account["as_of"]), timestamp(metrics["as_of"])
        account_state(account, now, 1.0)
        if not isinstance(cycle["records"], list) or len(cycle["records"]) > 100:
            raise ValueError("invalid cycle record count")
        parsed = [(record, proposal(record)) for record in cycle["records"]]
        if len({p.ticket_id for _, p in parsed}) != len(parsed):
            raise ValueError("duplicate ticket inside bundle")
        limits = limits or load_limits(ROOT / "config.json")
        watchlist = watchlist or load_watchlist(ROOT / "scout/watchlist.json")
        sleeves = {row.symbol: "core" for row in watchlist.core}
        sleeves.update({symbol: "satellite" for symbol in watchlist.satellites})
        instruments = {p.symbol: instrument(p.symbol, metrics) for _, p in parsed}
        payload_hash = digest(bundle)
        day = eastern(now).date().isoformat()

        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM artifacts WHERE file_id=?", (file_id,)).fetchone()
            if prior:
                if prior["session_id"] != session_id or prior["payload_hash"] != payload_hash:
                    raise ValueError("artifact identity collision")
                return json.loads(prior["result"])
            control = conn.execute("SELECT * FROM control").fetchone()
            baseline = conn.execute("SELECT * FROM baselines WHERE trading_day=?", (day,)).fetchone()
            reasons = []
            if control["mode"] != "DRY_RUN" or control["trading_enabled"] or not control["guard_halted"]:
                raise ValueError("coordinator safety interlock violated")
            if control["revoked_at"]:
                reasons.append("PILOT_REVOKED")
            starts, expires = timestamp(control["starts_at"]), timestamp(control["expires_at"])
            if not starts <= now < expires:
                reasons.append("PILOT_INACTIVE")
            if generated < starts:
                reasons.append("PRE_PILOT_CYCLE")
            if not baseline:
                reasons.append("BASELINE_MISSING")
            elif timestamp(baseline["observed_at"]) > now:
                reasons.append("BASELINE_IN_FUTURE")
            if any(not -300 <= (now - stamp).total_seconds() <= 1800
                   for stamp in (generated, account_at, metrics_at)):
                reasons.append("STALE_OR_FUTURE_SNAPSHOT")
            if any(eastern(stamp).date().isoformat() != day
                   for stamp in (generated, account_at, metrics_at)):
                reasons.append("WRONG_TRADING_DAY")
            if account_at > generated or metrics_at > generated:
                reasons.append("MIXED_SNAPSHOTS")
            slot = cycle["slot"]
            market = market_session(now)
            if (slot not in ("09:45", "11:00", "13:30") or due_slot(generated) != slot
                    or not allows_entries(slot, eastern(now).date())
                    or market["state"] != "OPEN"
                    or (timestamp(market["closes_at"]) - now).total_seconds() <= 1800
                    or cycle["entries_allowed"] is not True or cycle["trading_day"] is not True):
                reasons.append("ENTRY_SESSION_CLOSED")
            # The legacy account schema has no buy notionals; never assume its order count is a budget.
            if account["executed_orders_today"] or any(o["side"] == "BUY" for o in account["working_orders"]):
                reasons.append("BROKER_BUY_USAGE_UNVERIFIED")
            decisions = []
            for record, p in parsed:
                codes = list(reasons)
                if p.side != "BUY" or p.order_type != "LIMIT" or p.exit_reason:
                    codes.append("NOT_LIMIT_ENTRY")
                if sleeves.get(p.symbol) != record["sleeve"]:
                    codes.append("OUTSIDE_PILOT_UNIVERSE")
                if record["status"] not in ("AWAITING_APPROVAL", "READY"):
                    codes.append("SCOUT_NOT_ELIGIBLE")
                expected_prefix = f"P-{eastern(generated):%Y%m%d}-{slot.replace(':', '')}-"
                if (not p.ticket_id.startswith(expected_prefix)
                        or p.ticket_id.rsplit("-", 2)[-2] != p.symbol.removeprefix("US.")
                        or p.created_at != generated):
                    codes.append("TICKET_CYCLE_MISMATCH")
                if money(p.est_cost) != money(Decimal(str(p.limit_price)) * p.quantity):
                    codes.append("COST_MISMATCH")
                record_hash = digest(record)
                existing = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (p.ticket_id,)).fetchone()
                if existing:
                    if existing["record_hash"] != record_hash:
                        codes.append("TICKET_IDENTITY_COLLISION")
                    elif not codes:
                        decisions.append({**json.loads(existing["result"]), "status": "DRY_RUN_DUPLICATE"})
                        continue
                gate_codes = []
                if baseline and not codes:
                    state = account_state(account, now, baseline["equity_micro"] / 1_000_000)
                    reservations = conn.execute("SELECT * FROM reservations").fetchall()
                    pending = sum(r["notional_micro"] for r in reservations)
                    pending_fees = sum(r["fees_micro"] for r in reservations)
                    positions = dict(state.positions)
                    for row in reservations:
                        symbol = row["symbol"]
                        held = positions.get(symbol, Position(symbol, 0, 0, 0))
                        positions[symbol] = Position(symbol, held.quantity + row["quantity"],
                                                    held.market_value + row["notional_micro"] / 1_000_000,
                                                    held.average_cost)
                    state = replace(state, positions=positions,
                                    cash=state.cash - (pending + pending_fees) / 1_000_000,
                                    executed_orders_today=state.executed_orders_today + len(reservations),
                                    working_orders=state.working_orders + tuple(
                                        WorkingOrder(r["symbol"], "BUY") for r in reservations))
                    gate = evaluate(p, instruments[p.symbol], state, limits)
                    gate_codes = list(gate.codes)
                    codes.extend(c for c in gate_codes if c != "R11_NO_APPROVAL")
                    notional = money(Decimal(str(p.limit_price)) * p.quantity)
                    cap = int((Decimal(baseline["equity_micro"]) * BUDGET_SHARE).to_integral_value(rounding=ROUND_FLOOR))
                    if len(reservations) >= MAX_BUYS:
                        codes.append("PILOT_BUY_COUNT")
                    if pending + notional > cap:
                        codes.append("PILOT_BUY_BUDGET")
                result = {"ticket_id": p.ticket_id, "proposal_hash": p.canonical_hash(),
                          "status": "DRY_RUN_REJECTED" if codes else "DRY_RUN_RESERVED",
                          "codes": codes, "original_gate_codes": gate_codes,
                          "pilot_id": control["pilot_id"], "execution_blockers": EXECUTION_BLOCKERS,
                          "quantity": p.quantity, "limit_price": p.limit_price}
                if not codes:
                    conn.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 (p.ticket_id, p.canonical_hash(), record_hash, day, p.symbol, p.quantity,
                                  notional, money(estimate_fees(notional / 1_000_000, limits)),
                                  session_id, file_id, canonical(result)))
                if not existing:
                    conn.execute("INSERT INTO tickets VALUES(?,?,?)", (p.ticket_id, record_hash, canonical(result)))
                decisions.append(result)
                self._audit(conn, now, result["status"], {"ticket_id": p.ticket_id, "codes": codes})
            result = {"mode": "DRY_RUN", "trading_enabled": False, "guard_halted": True,
                      "session_id": session_id, "file_id": file_id, "decisions": decisions,
                      "bundle_codes": reasons}
            conn.execute("INSERT INTO artifacts VALUES(?,?,?,?)", (file_id, session_id, payload_hash, canonical(result)))
            self._audit(conn, now, "BUNDLE_PROCESSED", {"file_id": file_id, "decisions": len(decisions)})
            return result
