from __future__ import annotations

import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

from dashboard.manual import evaluate_scout, moment, normalize_account
from scout.marketdata import build_snapshot
from scout.settings import load_strategy
from scout.slots import REGULAR_OPEN, eastern, is_trading_day

CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def validate_opening_record(source_text: str, imported_at: str) -> dict:
    if not isinstance(source_text, str) or not 1 <= len(source_text.encode("utf-8")) <= 16384:
        raise ValueError("Invalid opening record size")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate opening record field")
            result[key] = value
        return result

    record = json.loads(source_text, object_pairs_hook=unique_object)
    fields = {"version", "kind", "account_id", "market", "currency", "trading_day", "as_of",
              "opening_equity", "source_reference", "source_evidence"}
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError("Invalid opening record fields")
    if (type(record["version"]) is not int or record["version"] != 1
            or record["kind"] != "broker_opening_equity" or record["account_id"] != "5829804"
            or type(record["market"]) is not int or record["market"] != 100 or record["currency"] != "USD"):
        raise ValueError("Opening record identity mismatch")
    equity = record["opening_equity"]
    if (not isinstance(equity, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?", equity)
            or Decimal(equity) <= 0):
        raise ValueError("Invalid opening equity")
    for key, maximum in (("source_reference", 300), ("source_evidence", 8000)):
        text = record[key]
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= maximum
                or any(ord(char) < 32 and char not in "\n\r\t" for char in text)):
            raise ValueError("Missing opening source evidence")
    observed, imported = moment(record["as_of"]), moment(imported_at)
    local = eastern(observed)
    if (record["trading_day"] != local.date().isoformat() or not is_trading_day(local.date())
            or local.time() != REGULAR_OPEN or observed > imported):
        raise ValueError("Opening record must value the account at the dated 09:30 ET open")
    return {"record": record, "source_text": source_text,
            "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "imported_at": imported.isoformat(), "verification": "operator_attested"}


def generate_tickets(payload: dict, run: dict, as_of: str, config: Path = CONFIG, baseline: dict | None = None) -> dict:
    if (payload["request_id"] != run["request_id"] or payload["mode"] != "scout"
            or payload["version"] != 1 or payload["account_id"] != "5829804" or payload["market"] != 100):
        raise ValueError("Scout request mismatch")
    now, started = moment(as_of), moment(run["started_at"])
    raw = payload["market_raw"]
    for stamp in [payload["as_of"], raw["as_of"], *(r.get("as_of", raw["as_of"]) for r in raw["symbols"].values())]:
        observed = moment(stamp)
        if (observed - started).total_seconds() < -5 or not -5 <= (now - observed).total_seconds() <= 1800:
            raise ValueError("Scout evidence is stale")
    settings = load_strategy(config)
    snapshot = build_snapshot(raw, settings.trend_fast_ema, settings.trend_slow_ema)
    account = normalize_account(payload, {})
    provenance = None
    if baseline is not None:
        checked = validate_opening_record(baseline["source_text"], baseline["imported_at"])
        if checked != baseline:
            raise ValueError("Opening record integrity mismatch")
        record = checked["record"]
        account_time = moment(payload["as_of"])
        if (record["trading_day"] == eastern(now).date().isoformat() == eastern(account_time).date().isoformat()
                and moment(record["as_of"]) <= min(now, account_time)):
            account.update(day_open_equity=float(record["opening_equity"]), baseline_verified=True)
            provenance = {key: checked[key] for key in ("sha256", "imported_at", "verification")}
            provenance.update({key: record[key] for key in ("trading_day", "as_of", "opening_equity", "source_reference")})
    cycle = evaluate_scout(snapshot, account, payload["news"], config, now)
    cycle.update(request_id=run["request_id"], review_only=True, trading_enabled=False,
                 baseline_verified=account["baseline_verified"], baseline=provenance,
                 daily_pnl_pct=account["equity"] / account["day_open_equity"] - 1 if provenance else None)
    return cycle


def main() -> int:
    try:
        source = sys.stdin.buffer.read(2_000_001)
        if len(source) > 2_000_000:
            raise ValueError("Input too large")
        request = json.loads(source)
        if request.get("action") == "validate_opening_record":
            try:
                result = validate_opening_record(request["source_text"], request["as_of"])
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError):
                result = {"error": "invalid_opening_record"}
        else:
            result = generate_tickets(request["payload"], request["run"], request["as_of"], baseline=request.get("baseline"))
        print(json.dumps(result, allow_nan=False))
        return 0
    except Exception:
        print("Scout ticket evaluation unavailable; no operational state changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
