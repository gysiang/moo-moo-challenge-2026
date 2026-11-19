---
name: moomoo-scout-cycle
description: Run one Moomoo x Qoder Scout trading cycle on paper account 5829804 - fetch US-market quotes, klines, news and account state, build EMA/SMA trend metrics, and emit dated BUY/SELL tickets through the deterministic rule gate. Use when a Scout slot fires (09:45, 11:00, 13:30, 15:30 ET), for an exit-only run between slots, or when the operator asks for a fresh paper-trading proposal set. Never places orders.
---

# Moomoo Scout cycle

Produces `runtime/cycle-<HHMM>.json`: deterministic tickets plus the gate verdict for
each. The Scout proposes; a human approves; the Guard submits.

Run from `/data/workspace/moo-moo-challenge-2026` in a cloud session, or from the local
checkout — all paths below are relative to that root.

## Preconditions

- moomoo MCP available: `quote_stock_quote`, `quote_cur_kline`, `quote_market_state`,
  `quote_trading_days`, `news_search`, and the read-only sim-trade tools.
- `scout/watchlist.json` exists (3 core ETFs + 18 satellites). Do not add symbols.
- Python ≥ 3.11; no packages to install.

## Sequence

```bash
python -m scout due                      # which slot is live right now
```

If `due_slot=none`, or it names a different slot than your task, stop and say so.

Check `quote_trading_days` and `quote_market_state`. Holiday, weekend or closed session:
print one line and stop.

Account snapshot — never guess:

```json
{"account_id": "5829804", "as_of": "<UTC now ISO>",
 "cash": 100000, "equity": 100000,
 "day_open_equity": 100000, "executed_orders_today": 0,
 "positions": {"US.SPY": {"quantity": 10, "market_value": 7400, "average_cost": 735}},
 "working_orders": [{"symbol": "US.SPY", "side": "BUY"}]}
```

`cash` from `sim_trade_cash_info.balance`, `equity` from `total_asset`,
`executed_orders_today` from today's order list, positions from `sim_trade_position_list`.
Write it to `runtime/account-5829804.json`. A failed call aborts the cycle. `as_of` is
mandatory: the dashboard and the Guard refuse snapshots older than
`guard_max_snapshot_age_minutes` (30 by default), as they do for the cycle's
`generated_at` and `metrics.as_of`.

Klines → metrics (scripts only — raw candles never go into the conversation):

1. For each watchlist symbol, `quote_cur_kline` with daily bars, last 60 sessions, saved
   straight to `/tmp/kl/<SYMBOL>.json`. Never `Read` a kline file back.
2. `quote_market_snapshot` once for the watchlist; `total_market_val` is the market cap.
3. Merge `/tmp/kl/*.json` plus the caps into `runtime/mcp-klines.json`:
   ```json
   {"as_of": "<UTC ISO>",
    "symbols": {"US.AAPL": {"instrument_type": "stock", "market_cap": 3520000000000,
      "klines": [{"time_key": "...", "close": 352.78, "high": 355.1}]}}}
   ```
   Keep the raw candle objects; `scout.ingest` maps `close`/`close_price`/`last_price`
   and `high`/`high_price`, sorts by `time_key`/`time`/`date`, and rejects a symbol with
   fewer than 50 closes or 20 highs. The merge script prints only `symbols N omitted [...]`.
4. `python -m scout.ingest --in runtime/mcp-klines.json --out runtime/market-raw.json`
5. `python -m scout snapshot --raw runtime/market-raw.json --out runtime/metrics.json`

Read `trend_up` per symbol: it is `ema_fast > ema_slow` (EMA8 > EMA21 from `config.json`).
`sma50` is still printed and still governs exits, along with the -8% stop and the same EMA
cross. Do not recompute or reinterpret these numbers.

News (BUY evidence only):

```bash
# one news_search per trend_up symbol, plain ASCII keyword "Company Ticker"
# (never HTML-escape &, never repeat the query in another language)
# save every reply as received in runtime/mcp-news.json: {"US.AAPL": <reply>, ...}
python -m scout.news --in runtime/mcp-news.json --out runtime/news.json
```

`scout.news` rewrites epoch milliseconds/seconds to UTC ISO, prefixes ids with `N-`,
sorts newest first, keeps three per symbol and drops items older than 24h or stamped in
the future. Those are exactly the items the gate would reject, so never hand-write or
edit `runtime/news.json`. A `kept 0` line is a HOLD, not something to work around.

Plan:

```bash
python -m scout plan --slot 09:45 --raw runtime/market-raw.json \
    --account runtime/account-5829804.json --news runtime/news.json \
    --approvals runtime/approvals.json --out runtime/cycle-0945.json
```

Omit `--news` or `--approvals` when the file does not exist. The planner reads
`runtime/exit-mandate.json` automatically when it exists: a covered full-position SELL comes
back `READY` instead of `AWAITING_APPROVAL`, and every SELL record carries an `exit_reason`.
Entry approvals last 12 hours (`approval_ttl_minutes`).

Deliver (mandatory, every run including exit-only and empty cycles): call DeliverArtifacts
once with the cycle file you wrote, `runtime/metrics.json` and `runtime/news.json`. The
operator dashboard syncs only delivered artifacts; without them the tickets never reach
the operator and cannot be approved. Never deliver `account-5829804.json`, kline dumps or
credential-bearing files. If delivery fails, report it in one line; do not retry silently.

## Exit-only run

Protects open positions between the entry slots. Do the market-state, account, kline and
metrics steps above (skip `scout due` and the news step), then:

```bash
python -m scout plan --exit-only --raw runtime/market-raw.json \
    --account runtime/account-5829804.json --approvals runtime/approvals.json \
    --out runtime/cycle-<HHMM>.json
```

No `--slot`: the label defaults to the current ET time, entries are blocked, and only held
positions are examined. With an active operator mandate the exits are `READY` and the Guard
executes them on its own exit-only schedule; without one they are `AWAITING_APPROVAL`.
Write the report even when there is nothing to exit — an empty record list means "no action".

## Reading the result

| Status | Meaning | Next |
|---|---|---|
| `READY` | passed every rule, via a hash-bound approval or an operator exit mandate | Guard may execute |
| `AWAITING_APPROVAL` | failed only R11 | operator approves in the dashboard or `python -m scout approve` |
| `REJECTED` | any other rule, e.g. `R2_SIZE`, `R10_EVIDENCE_STALE` | log it, do not retry |

Ticket ids are `P-YYYYMMDD-HHMM-SYMBOL-NN`; re-running the same slot reproduces the same
ids, which is what makes retries idempotent.

## Boundaries

- No order tools. No approval commands. No edits to `config.json`, `watchlist.json`,
  `gate/`, or tests.
- Never write, edit, or delete `runtime/exit-mandate.json`, and never pass `--mandate ""`
  to make an exit look authorized. Granting and revoking it is an operator command
  (`python -m guard.mandate_cli`).
- Read-only `sim_trade_*` calls only: `sim_trade_cash_info`, `sim_trade_position_list`,
  `sim_trade_history_order_list`, `sim_trade_account_list`.
- 15:30 ET is exits-only; the planner blocks entries and the report says so.
- Omit symbols with insufficient history and list them at the end; never pad a series.
- Write `runtime/cycle-<HHMM>-rerun.json` rather than overwriting an earlier cycle file.
- Keep tool output short: no raw JSON, candle arrays or article bodies in the transcript,
  and never read back a file you just wrote.

## Done when

`runtime/cycle-<HHMM>.json` exists, the ticket table was printed unchanged, the cycle plus
`metrics.json` and `news.json` were handed to DeliverArtifacts (or the failure was
reported), and the final line lists omitted symbols and any failed data source.
