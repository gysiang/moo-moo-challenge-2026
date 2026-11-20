from __future__ import annotations

import json
import sys
from pathlib import Path

from dashboard.manual import evaluate_scout, moment, normalize_account
from scout.marketdata import build_snapshot
from scout.settings import load_strategy

CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def generate_tickets(payload: dict, run: dict, as_of: str, config: Path = CONFIG) -> dict:
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
    # A current balance is not a verified day-opening baseline.
    account = normalize_account(payload, {})
    cycle = evaluate_scout(snapshot, account, payload["news"], config, now)
    cycle.update(request_id=run["request_id"], review_only=True, trading_enabled=False, baseline_verified=False)
    return cycle


def main() -> int:
    try:
        source = sys.stdin.buffer.read(2_000_001)
        if len(source) > 2_000_000:
            raise ValueError("Input too large")
        request = json.loads(source)
        cycle = generate_tickets(request["payload"], request["run"], request["as_of"])
        print(json.dumps(cycle, allow_nan=False))
        return 0
    except Exception:
        print("Scout ticket evaluation unavailable; no operational state changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
