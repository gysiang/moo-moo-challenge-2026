---
name: moomoo-paper-reconciliation
description: Reconcile Moomoo paper-account state against the Guard event log and reproduce a stored cycle byte-for-byte - rebuild canonical state from append-only JSONL, summarize submitted vs filled vs position, and flag halts, kill switches and guardrail breaches. Use at end of day, before resuming after a halt, and when preparing submission evidence for the challenge.
---

# Moomoo paper reconciliation

Evidence, not vibes: the JSONL log is the source of truth, `replay()` turns it back into
state, and the same log replays to the same bytes.

Run from `/data/workspace/moo-moo-challenge-2026` in a cloud session (the local checkout
otherwise).

## Event vocabulary

`ORDER_SUBMITTED`, `ORDER_REJECTED`, `ORDER_BLOCKED`, `ORDER_EXISTING`, `ORDER_UNKNOWN`,
`HALTED`, `RECONCILED`, `KILL_SWITCH`, `RESUME` — written by `guard/execution.py` (local
Guard) or `guard/bridge.py` (cloud Guard). Never hand-edit the file; append only.

## Canonical replay (T8)

```bash
python -c "from guard.events import replay; print(replay('runtime/guard-events.jsonl').canonical())"
```

`canonical()` is JSON with `sort_keys` and compact separators, so two invocations that
read the same log must print identical strings. Store the hash in the day's summary:

```bash
python -c "import hashlib;from guard.events import replay;print(hashlib.sha256(replay('runtime/guard-events.jsonl').canonical().encode()).hexdigest())"
```

## Daily checklist

1. **Submissions vs fills.** For every `ORDER_SUBMITTED`, confirm a later `RECONCILED` with
   the same `order_id`. `unfilled = submitted − filled`; a non-zero value is a partial fill,
   not an error, but it must appear in the trade blotter.
2. **Positions.** Broker `sim_trade_position_list` quantities must match the last
   `RECONCILED.position_quantity` per symbol. Mismatch → do not trade until resolved.
3. **Counts.** Executed orders today ≤ 20, open positions ≤ 12, single-name ≤ 40% of
   equity. Breach → halt new entries for the day and log the breach.
4. **Halts and kills.** `replay(...).halted` or `.kill_switch_engaged` true means order flow
   is latched off. It clears only via `python -m guard.bridge_cli resume --note "<how it was
   reconciled>"` after step 2 passes.
5. **Rejections.** Every `ORDER_REJECTED` keeps its rule codes; do not delete the bad days.
   The deck's "what failed and why" section is built from these lines.
6. **Approval coverage.** Orders per day must have an approval record dated before the
   submission. Approval coverage < 100% is a G3 failure — escalate, do not smooth it over.

## Summaries to hand the operator

- Blotter line per order: time, ticket, symbol, side, qty, limit, filled, unfilled, status, order_id.
- Decision line per rejected ticket: ticket, rule code, one-sentence reason.
- Day line: equity, cash, daily P&L %, orders used/20, positions used/12, replay sha256.

## Rules

- Read-only over broker state; this skill never submits or cancels.
- If the log and the broker disagree, broker wins and trading stops (PRD §10).
- Keep `runtime/guard-events.jsonl` in version control; it is the submission artefact.
