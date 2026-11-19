---
name: moomoo-guard-execution
description: Execute authorized Moomoo paper orders on account 5829804 through the deterministic gate - re-run R1-R11 with guard.bridge_cli, submit the exact sim_trade_input_order arguments, record the broker reply, and halt on any unconfirmed outcome. Use when the operator supplies approved ticket ids, or on a scheduled exit-only run while a valid operator exit mandate is on disk. Never originates trades and never retries a submit.
---

# Moomoo Guard execution

The only code path that places paper orders. Gate first, always: a rejection is logged
and never reaches the broker.

All commands run from `/data/workspace/moo-moo-challenge-2026` in a cloud session (the
local checkout otherwise).

## Inputs

- `runtime/cycle-<HHMM>.json` — Scout report containing the ticket.
- `runtime/approvals.json` — written by the human (`python -m scout approve` or the dashboard).
- `runtime/exit-mandate.json` — optional operator-signed mandate for full-position risk-reducing
  exits. Read it with `python -m guard.mandate_cli show`; never write or edit it.
- `runtime/account-5829804.json`, `runtime/metrics.json` — current account and market facts.
  All three snapshots (cycle `generated_at`, account `as_of`, metrics `as_of`) must be less
  than 30 minutes old; otherwise stop and report "stale snapshot".
- The ticket id from the operator's task, or the READY SELL records of an exit-only run.
  Never derive a ticket yourself.

## Step 1 — re-run the gate

```bash
python -m guard.bridge_cli prepare --cycle runtime/cycle-0945.json --ticket <T> \
    --account runtime/account-5829804.json --approvals runtime/approvals.json \
    --metrics runtime/metrics.json --out runtime/order-plan.json
```

Exit `0` + `"status": "READY"` → continue. Exit `2` + `"status": "REJECTED"` → print the
codes (`R11_NO_APPROVAL` for a missing/expired/edited ticket or an exit the mandate does not
cover, `R4_FUNDS`, `R5_CONCENTRATION`, `R9_DUPLICATE`, …) and stop for that ticket.

A READY plan names its authorization: `"authorization": "APPROVAL"` (hash-bound human
approval) or `"authorization": "MANDATE"` plus `"mandate_id"` (operator exit mandate). It
must match what started the run.

The plan contains the exact MCP call:

```json
{"ticket_id": "P-20261118-0945-AAPL-01",
 "arguments": {"acc_id": "5829804", "market": 100, "symbol": "US.AAPL",
               "order_type": 1, "order_side": 1, "qty": "14",
               "price": "231.4000", "text": "qdr:P-20261118-0945-AAPL-01"}}
```

## Step 2 — submit once

Call `sim_trade_input_order` with `arguments` **verbatim**. No field changes, no rounding,
no market orders, no splitting. Save the raw reply to `runtime/order-result.json`.

## Step 3 — record

```bash
python -m guard.bridge_cli record --order runtime/order-plan.json \
    --result runtime/order-result.json --log runtime/guard-events.jsonl
```

| Output | Meaning | Action |
|---|---|---|
| `{"status": "SUBMITTED", "order_id": "..."}` | broker acknowledged | reconcile |
| `{"status": "REJECTED", "reason": "broker: ..."}` | explicit refusal | next ticket; retry needs a new ticket |
| `{"status": "UNKNOWN"}` | no order id / malformed reply; log has `ORDER_UNKNOWN` + `HALTED` | **stop the run** |

`record` refuses a plan that is not `READY`, so a rejected ticket can never produce a
submission event.

## Step 4 — reconcile

```bash
python -m guard.bridge_cli reconcile --log runtime/guard-events.jsonl \
    --order-id <id> --submitted <n> --filled <n> --status <FILLED|PARTIAL|...> \
    --position-qty <n>
```

Fill and position come from `sim_trade_history_order_list` and `sim_trade_position_list`.
A 100-share order that fills 60 must show `unfilled: 40`, `position_quantity: 60`.

## Step 5 — kill switch (operator request, or after a halt)

```bash
# cancel every working order first with sim_trade_cancel_order
python -m guard.bridge_cli kill --log runtime/guard-events.jsonl --reason "<why>" \
    --cancelled <id,id> --failed <id>
```

Latched: no further submission this run. `resume` is an operator command:
`python -m guard.bridge_cli resume --log runtime/guard-events.jsonl --note "<reconciled how>"`.

## Scheduled exit-only run

The only scheduled run Guard accepts, so a stop-loss or SMA50 break is acted on while the
operator is asleep.

1. `python -m guard.mandate_cli show` — no mandate, expired mandate, or hash mismatch →
   submit nothing and report that exits need per-ticket approval.
2. Newest `runtime/cycle-*.json`; stop with "stale snapshot" if cycle `generated_at`,
   account `as_of`, or `metrics.as_of` is older than 30 minutes. Never fetch market data
   yourself to refresh it.
3. Take only `side: "SELL"` records with a non-empty `exit_reason` and `status: "READY"`.
   Ignore BUYs, partial quantities, and `AWAITING_APPROVAL`.
4. Steps 1-4 per ticket in report order, at most `max_exits_per_run`. `prepare` counts
   exits already submitted for this run key in `runtime/guard-events.jsonl` and rejects
   the rest with `R11_NO_APPROVAL`; report those as skipped, never as failures to work
   around.
5. Report the mandate id and how many exits it allowed.

An unconfirmed broker outcome still halts everything, mandated exits included.

## Rules

- Paper account `5829804`, market `100`. Any other account id → refuse.
- One submit attempt per ticket, ever. Idempotency comes from the deterministic
  `qdr:<ticket>` note and the ticket id, not from retrying.
- Broker vs plan disagreement (cash, position) → stop, log, report.
- The event log is append-only; never truncate or rewrite `runtime/guard-events.jsonl`.
- Never grant, extend, edit, or revoke `runtime/exit-mandate.json`; `mandate_cli grant`
  and `revoke` are operator commands. `show` only.
- Entries always need a hash-bound human approval. A mandate never covers a BUY, a partial
  trim, or a new position. Any other scheduled run (entries, adds, catch-up) → refuse.

## Done when

Every requested ticket has one line — `SUBMITTED <order_id>` / `REJECTED <code>` /
`UNKNOWN` / `SKIPPED <reason>` — plus its reconciliation numbers, and the event-log path
is printed. If a halt occurred, it is the first line of your report.
