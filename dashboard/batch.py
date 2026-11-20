from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from dashboard import state
from dashboard.cloud import CloudError
from dashboard.guard import CYCLE_FILE, TICKET_ID, snapshot_age_error
from dashboard.manual import GUARD_TEMPLATE, IDENTITY, account_state, atomic_json, moment, quantity
from scout.slots import eastern, market_session
from gate import Approval, Position, WorkingOrder, load_limits
from guard.bridge import prepare_order, proposal_from_record


class BatchApprovals:
    def __init__(self, app):
        self.app = app
        self.path = app.runtime / "dashboard-batch.json"
        self.protocol_ready = False
        self.protocol_checked = False

    def check_protocol(self):
        self.protocol_checked = True
        try:
            self.protocol_ready = bool(self.app.cloud and self.app.cloud.guard_batch_ready(GUARD_TEMPLATE))
        except CloudError:
            self.protocol_ready = False
        return self.protocol_ready

    def status(self):
        stored = state.read_json(self.path)
        if not isinstance(stored, dict) or "status" not in stored:
            stored = {"status": "unknown" if self.path.exists() else "idle"}
        return {**stored, "protocol_ready": self.protocol_ready}

    def approve(self, payload: dict, now=None) -> dict:
        app = self.app
        now = now or datetime.now(timezone.utc)
        if app.demo or app.cloud is None or app.guard is None:
            return {"ok": False, "error": "Paper Guard is not configured; demo mode never dispatches orders."}
        with app.lock:
            try:
                envelope, approvals = self.prepare(payload, now)
                if not self.check_protocol():
                    raise ValueError("Cloud Guard batch protocol is not installed or could not be verified. No approvals were written.")
            except (ValueError, KeyError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
            ticket_ids = [t["ticket_id"] for t in envelope["tickets"]]
            quantities = {r["ticket_id"]: r["quantity"] for cycle in envelope["cycles"].values()
                          for r in cycle["records"] if r["ticket_id"] in ticket_ids}
            batch = {"batch_id": envelope["batch_id"], "status": "creating", "ticket_ids": ticket_ids,
                     "quantities": quantities, "started_at": now.isoformat()}
            atomic_json(self.path, batch)
            existing = state.load_approvals(app.runtime / app.approvals_file)
            existing.update(approvals)
            atomic_json(app.runtime / app.approvals_file, existing)
            for ticket in envelope["tickets"]:
                state.append_jsonl(app.runtime / app.triggers_file,
                                   {**ticket, "state": "sending", "requested_at": now.isoformat(), "batch_id": batch["batch_id"]})
            try:
                session = app.cloud.create_forward_session(IDENTITY, GUARD_TEMPLATE, f"Approved batch {batch['batch_id']}")
                batch.update(session_id=session["id"], status="sending")
                atomic_json(self.path, batch)
                app.cloud.send_forward_message(session["id"], json.dumps(envelope, sort_keys=True))
                batch["status"] = "accepted"
            except CloudError as exc:
                batch.update(status="unknown", error="Batch delivery is unconfirmed. Inspect the session; never resend these tickets. " + exc.message)
            atomic_json(self.path, batch)
            for ticket in envelope["tickets"]:
                state.append_jsonl(app.runtime / app.triggers_file,
                                   {**ticket, "state": batch["status"], "requested_at": now.isoformat(),
                                    "session_id": batch.get("session_id"), "batch_id": batch["batch_id"]})
            return {"ok": batch["status"] == "accepted", **batch}

    def prepare(self, payload: dict, now: datetime):
        app = self.app
        current = app.state()
        current["market"] = market_session(now)
        if not (app.runtime / app.events_file).is_file():
            raise ValueError("Guard journal is missing; reconcile before trading.")
        if current["guard"]["halted"] or current["guard"]["kill_switch"]:
            raise ValueError("Guard is halted. Reconciliation and explicit operator clearance are required; no approval was written.")
        if self.status()["status"] not in ("idle", "completed"):
            raise ValueError("A prior batch is unresolved; reconcile it before approving another.")
        if current["market"]["state"] != "OPEN":
            raise ValueError("US regular market is closed.")
        selected = payload.get("tickets")
        if not isinstance(selected, list) or not 1 <= len(selected) <= 20:
            raise ValueError("Select between 1 and 20 tickets.")
        snapshot = state.read_json(app.runtime / "dashboard-snapshot.json") or {}
        account = snapshot.get("account") or state.read_json(app.runtime / "account-5829804.json") or {}
        metrics = snapshot.get("metrics") or state.read_json(app.runtime / "metrics.json") or {}
        if account.get("account_id") != "5829804":
            raise ValueError("Paper account mismatch.")
        last_batch = self.status()
        if last_batch.get("completed_at") and moment(account["as_of"]) <= moment(last_batch["completed_at"]):
            raise ValueError("Refresh broker state after the previous batch before approving more orders.")
        if not account.get("orders_complete") or eastern(moment(account["as_of"])).date() != eastern(now).date():
            raise ValueError("A complete broker snapshot from the current ET date is required.")
        if not account.get("baseline_verified") or not account.get("day_open_equity"):
            raise ValueError("Verified day-opening equity is unavailable. Trading remains blocked.")
        if any(o.get("status") == "UNKNOWN" for o in account.get("working_orders", [])):
            raise ValueError("Broker has an order with unknown status; reconcile first.")
        virtual = account_state(account, now)
        for order in account.get("working_orders", []):
            remaining = order["quantity"] - order["filled_quantity"]
            positions = dict(virtual.positions)
            cash = virtual.cash
            if order["side"] == "BUY":
                cost = remaining * order["limit_price"]
                old = positions.get(order["symbol"], Position(order["symbol"], 0, 0, 0))
                positions[old.symbol] = replace(old, quantity=old.quantity + remaining, market_value=old.market_value + cost)
                cash -= cost
            virtual = replace(virtual, cash=cash, positions=positions)
        approvals, cycles, tickets = {}, {}, []
        seen_symbols = set()
        triggers = state.load_triggers(app.runtime / app.triggers_file)
        rejections = state.load_rejections(app.runtime / app.rejections_file)
        limits = load_limits(app.config_path)
        for choice in selected:
            if not isinstance(choice, dict):
                raise ValueError("Invalid ticket selection.")
            ticket, name = choice.get("ticket_id", ""), choice.get("cycle_file", "")
            if not isinstance(ticket, str) or not isinstance(name, str) or not TICKET_ID.fullmatch(ticket) or not CYCLE_FILE.fullmatch(name):
                raise ValueError("Invalid ticket or cycle name.")
            if ticket in approvals or ticket in triggers or ticket in rejections or any(o.get("ticket_id") == ticket for o in account.get("orders", [])):
                raise ValueError(f"{ticket}: duplicate, previously dispatched or rejected.")
            cycle = state.read_json(app.runtime / name)
            if not cycle:
                raise ValueError("Cycle is missing.")
            if cycle.get("request_id") and cycle["request_id"] != snapshot.get("scout_request_id"):
                raise ValueError("Ticket is not from the latest committed Scout result; reload and review.")
            stale = snapshot_age_error(cycle, account, metrics, now, current["limits"]["guard_max_snapshot_age_minutes"])
            if stale:
                raise ValueError(stale)
            record = next((r for r in cycle["records"] if r["ticket_id"] == ticket), None)
            if record is None or record["status"] != "AWAITING_APPROVAL":
                raise ValueError(f"{ticket}: not awaiting approval.")
            if record["side"] == "BUY" and moment(current["market"]["closes_at"]) - now <= timedelta(minutes=30):
                raise ValueError("No new entries during the last 30 minutes.")
            proposal = proposal_from_record(record)
            if proposal.canonical_hash() != record["proposal_hash"] or choice.get("proposal_hash") != record["proposal_hash"]:
                raise ValueError(f"{ticket}: proposal changed; reload and review it.")
            if proposal.symbol in seen_symbols:
                raise ValueError("Only one ticket per symbol may be selected in a batch.")
            seen_symbols.add(proposal.symbol)
            approval = Approval(ticket, proposal.canonical_hash(), now, now + limits.approval_ttl)
            plan = prepare_order(record, replace(virtual, approval=approval), limits, metrics=metrics)
            if plan["status"] != "READY":
                raise ValueError(f"{ticket}: {' '.join(plan['gate']['codes'])}")
            approvals[ticket] = {"proposal_hash": approval.proposal_hash, "approved_at": now.isoformat(),
                                 "expires_at": approval.expires_at.isoformat()}
            cycles[name] = cycle
            tickets.append({"ticket_id": ticket, "cycle_file": name, "proposal_hash": proposal.canonical_hash()})
            positions = dict(virtual.positions)
            cash = virtual.cash
            if proposal.side == "BUY":
                old = positions.get(proposal.symbol, Position(proposal.symbol, 0, 0, 0))
                positions[proposal.symbol] = replace(old, quantity=old.quantity + proposal.quantity,
                                                     market_value=old.market_value + proposal.est_cost)
                cash -= proposal.est_cost
            virtual = replace(virtual, cash=cash, positions=positions,
                              executed_orders_today=virtual.executed_orders_today + 1,
                              working_orders=virtual.working_orders + (WorkingOrder(proposal.symbol, proposal.side),))
        envelope = {"kind": "guard-batch", "version": 1, "batch_id": uuid.uuid4().hex,
                    "requested_by": "dashboard-operator", "requested_at": now.isoformat(),
                    "account_id": "5829804", "market": 100, "tickets": tickets,
                    "cycles": cycles, "account": account, "metrics": metrics, "approvals": approvals}
        if len(json.dumps(envelope)) > 200_000:
            raise ValueError("Batch evidence is too large; select fewer tickets.")
        return envelope, approvals

    def poll(self):
        app = self.app
        with app.lock:
            if not self.protocol_checked:
                self.check_protocol()
            batch = self.status()
            if batch.get("outcomes") or batch["status"] not in ("creating", "sending", "accepted", "unknown") or not batch.get("session_id"):
                return batch
            try:
                name = f"guard-batch-{batch['batch_id']}.json"
                files = app.cloud.files(scope_id=batch["session_id"], name=name, limit=100)
                matches = [f for f in files if f.get("filename") == name and
                           (f.get("scope") or {}).get("id") == batch["session_id"]]
                if len(matches) != 1:
                    return batch
                blob = app.cloud.download_file(matches[0]["id"])
                if len(blob) > 200_000:
                    raise ValueError("result too large")
                result = json.loads(blob)
                if result.get("version") != 1 or result.get("batch_id") != batch["batch_id"] or result.get("account_id") != "5829804":
                    raise ValueError("result scope mismatch")
                outcomes = result["outcomes"]
                if len(outcomes) != len(batch["ticket_ids"]) or {o["ticket_id"] for o in outcomes} != set(batch["ticket_ids"]):
                    raise ValueError("incomplete batch result")
                if any(o["status"] not in ("RECONCILED", "REJECTED", "SKIPPED", "UNKNOWN") for o in outcomes):
                    raise ValueError("invalid outcome")
                for outcome in outcomes:
                    if outcome["status"] == "RECONCILED":
                        submitted, filled, unfilled = (quantity(outcome[k]) for k in ("submitted", "filled", "unfilled"))
                        if (not outcome.get("order_id") or submitted == 0 or submitted != filled + unfilled
                                or submitted != batch["quantities"][outcome["ticket_id"]]):
                            raise ValueError("invalid reconciliation quantities")
                halted = result.get("halted") is not False or any(o["status"] == "UNKNOWN" for o in outcomes)
                if halted:
                    state.append_jsonl(app.runtime / app.events_file, {"event": "HALTED", "reason": "Cloud batch requires reconciliation", "batch_id": batch["batch_id"]})
                for outcome in outcomes:
                    state.append_jsonl(app.runtime / app.triggers_file, {**outcome, "state": outcome["status"].lower(),
                                                                       "session_id": batch["session_id"], "batch_id": batch["batch_id"]})
                batch.update(status="unknown" if halted else "completed", outcomes=outcomes,
                             completed_at=datetime.now(timezone.utc).isoformat())
                atomic_json(self.path, batch)
            except (CloudError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
                batch["error"] = "Batch result unavailable or invalid; inspect Guard before any further approval."
                atomic_json(self.path, batch)
            return batch
