---
name: moomoo-guard-execution
description: Execute human-approved Moomoo paper orders on account 5829804 through the deterministic gate - re-run R1-R11 with guard.bridge_cli, submit the exact sim_trade_input_order arguments, record the broker reply, and halt on any unconfirmed outcome. Use when the operator supplies approved ticket ids. Never originates trades and never retries a submit.
---

# Moomoo Guard execution

The only code path that places paper orders. Gate first, always: a rejection is logged
and never reaches the broker.

All commands run from `/workspace/challenge-agent`.

## Inputs

- `runtime/cycle-<HHMM>.json` — Scout report containing the ticket.
- `runtime/approvals.json` — written by the human (`python -m scout approve`).
- `runtime/account-5829804.json`, `runtime/metrics.json` — current account and market facts.
- The ticket id from the operator's task. Never derive one yourself.

## Step 1 — re-run the gate

```bash
python -m guard.bridge_cli prepare --cycle runtime/cycle-0945.json --ticket <T> \
    --account runtime/account-5829804.json --approvals runtime/approvals.json \
    --metrics runtime/metrics.json --out runtime/order-plan.json
```

Exit `0` + `"status": "READY"` → continue. Exit `2` + `"status": "REJECTED"` → print the
codes (`R11_NO_APPROVAL` for a missing/expired/edited ticket, `R4_FUNDS`, `R5_CONCENTRATION`,
`R9_DUPLICATE`, …) and stop for that ticket.

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

## Rules

- Paper account `5829804`, market `100`. Any other account id → refuse.
- One submit attempt per ticket, ever. Idempotency comes from the deterministic
  `qdr:<ticket>` note and the ticket id, not from retrying.
- Broker vs plan disagreement (cash, position) → stop, log, report.
- The event log is append-only; never truncate or rewrite `runtime/guard-events.jsonl`.

## Done when

Every requested ticket has one line — `SUBMITTED <order_id>` / `REJECTED <code>` /
`UNKNOWN` / `SKIPPED <reason>` — plus its reconciliation numbers, and the event-log path
is printed. If a halt occurred, it is the first line of your report.
