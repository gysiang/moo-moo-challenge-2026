from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CLOSE_KEYS = ("close", "close_price", "last_price", "cur_price", "price")
HIGH_KEYS = ("high", "high_price")
TIME_KEYS = ("time_key", "time", "date", "timestamp")


def _first(row: dict[str, Any], keys: tuple[str, ...], label: str, symbol: str) -> float:
    for key in keys:
        if key in row and row[key] is not None:
            return float(row[key])
    raise ValueError(f"{symbol}: kline row has no {label} field ({', '.join(keys)})")


def _time(row: dict[str, Any]):
    for key in TIME_KEYS:
        if key in row and row[key] is not None:
            return row[key]
    return None


def symbol_row(row: dict[str, Any], symbol: str) -> dict[str, Any]:
    if "closes" in row and "highs" in row:
        closes = [float(x) for x in row["closes"]]
        highs = [float(x) for x in row["highs"]]
    else:
        klines = row.get("klines")
        if not isinstance(klines, list):
            raise ValueError(f"{symbol}: needs 'closes'/'highs' or a 'klines' list")
        if klines and all(_time(k) is not None for k in klines):
            klines = sorted(klines, key=lambda k: str(_time(k)))
        closes = [_first(k, CLOSE_KEYS, "close", symbol) for k in klines]
        highs = [_first(k, HIGH_KEYS, "high", symbol) for k in klines]
    if len(closes) < 50:
        raise ValueError(f"{symbol}: {len(closes)} closes, need at least 50 daily bars")
    if len(highs) < 20:
        raise ValueError(f"{symbol}: {len(highs)} highs, need at least 20 daily bars")
    return {
        "instrument_type": row.get("instrument_type", "stock"),
        "market_cap": float(row["market_cap"]),
        "us_listed": bool(row.get("us_listed", True)),
        "tradable": bool(row.get("tradable", True)),
        "as_of": row.get("as_of"),
        "closes": closes,
        "highs": highs,
    }


def normalise(payload: dict[str, Any]) -> dict[str, Any]:
    if "as_of" not in payload or "symbols" not in payload:
        raise ValueError("input needs 'as_of' and 'symbols'")
    if not payload["symbols"]:
        raise ValueError("input has no symbols")
    symbols = {
        symbol: symbol_row(row, symbol)
        for symbol, row in payload["symbols"].items()
    }
    for row in symbols.values():
        if row["as_of"] is None:
            row.pop("as_of")
    return {"as_of": payload["as_of"], "symbols": symbols}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scout.ingest",
        description="Convert MCP quote_cur_kline dumps into scout market-raw JSON",
    )
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.source).read_text(encoding="utf-8"))
    raw = normalise(payload)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    for symbol, row in sorted(raw["symbols"].items()):
        print(f"  {symbol:<9} {len(row['closes'])} closes, {len(row['highs'])} highs")
    print(f"raw -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
