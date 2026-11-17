---
name: moomoo-scout-cycle
description: Run one Moomoo x Qoder Scout trading cycle on paper account 5829804 - fetch US-market quotes, klines, news and account state, build SMA/trend metrics, and emit dated BUY/SELL tickets through the deterministic rule gate. Use when a Scout slot fires (09:45, 11:00, 13:30, 15:30 ET) or when the operator asks for a fresh paper-trading proposal set. Never places orders.
---

# Moomoo Scout cycle

Produces `runtime/cycle-<HHMM>.json`: deterministic tickets plus the gate verdict for
each. The Scout proposes; a human approves; the Guard submits.

Run from `/workspace/challenge-agent` (or the local checkout — paths are relative).

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
{"account_id": "5829804", "cash": 100000, "equity": 100000,
 "day_open_equity": 100000, "executed_orders_today": 0,
 "positions": {"US.SPY": {"quantity": 10, "market_value": 7400, "average_cost": 735}},
 "working_orders": [{"symbol": "US.SPY", "side": "BUY"}]}
```

`cash` from `sim_trade_cash_info.balance`, `equity` from `total_asset`,
`executed_orders_today` from today's order list, positions from `sim_trade_position_list`.
Write it to `runtime/account-5829804.json`. A failed call aborts the cycle.

Klines → metrics:

1. For each watchlist symbol, `quote_cur_kline` with daily bars, last 60 sessions.
2. Write `runtime/mcp-klines.json`:
   ```json
   {"as_of": "<UTC ISO>",
    "symbols": {"US.AAPL": {"instrument_type": "stock", "market_cap": 3520000000000,
      "klines": [{"time_key": "...", "close": 352.78, "high": 355.1}]}}}
   ```
   Keep the raw candle objects; `scout.ingest` maps `close`/`close_price`/`last_price`
   and `high`/`high_price`, sorts by `time_key`/`time`/`date`, and rejects a symbol with
   fewer than 50 closes or 20 highs.
3. `python -m scout.ingest --in runtime/mcp-klines.json --out runtime/market-raw.json`
4. `python -m scout snapshot --raw runtime/market-raw.json --out runtime/metrics.json`

News (BUY evidence only):

```bash
# call news_search for every symbol whose metrics line shows trend_up=True
# runtime/news.json:
# {"US.AAPL": [{"id": "N-a41f", "as_of": "2026-11-18T13:05:00Z",
#               "source": "moomoo.news_search", "title": "..."}]}
```

Use the article's real publication time. Missing or stale news is a legitimate
`R10_EVIDENCE_STALE` rejection — it is how "missing evidence ⇒ HOLD" is enforced.

Plan:

```bash
python -m scout plan --slot 09:45 --raw runtime/market-raw.json \
    --account runtime/account-5829804.json --news runtime/news.json \
    --approvals runtime/approvals.json --out runtime/cycle-0945.json
```

Omit `--news` or `--approvals` when the file does not exist.

## Reading the result

| Status | Meaning | Next |
|---|---|---|
| `READY` | passed every rule including approval | Guard may execute |
| `AWAITING_APPROVAL` | failed only R11 | operator runs `python -m scout approve` |
| `REJECTED` | any other rule, e.g. `R2_SIZE`, `R10_EVIDENCE_STALE` | log it, do not retry |

Ticket ids are `P-YYYYMMDD-HHMM-SYMBOL-NN`; re-running the same slot reproduces the same
ids, which is what makes retries idempotent.

## Boundaries

- No order tools. No approval commands. No edits to `config.json`, `watchlist.json`,
  `gate/`, or tests.
- 15:30 ET is exits-only; the planner blocks entries and the report says so.
- Omit symbols with insufficient history and list them at the end; never pad a series.
- Write `runtime/cycle-<HHMM>-rerun.json` rather than overwriting an earlier cycle file.

## Done when

`runtime/cycle-<HHMM>.json` exists, the ticket table was printed unchanged, and the final
line lists omitted symbols and any failed data source.
