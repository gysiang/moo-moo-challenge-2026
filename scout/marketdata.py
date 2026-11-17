from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scout.models import Metrics, Snapshot


def _parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def metrics_from_history(
    symbol: str,
    instrument_type: str,
    market_cap: float,
    closes: list[float],
    highs: list[float],
    as_of: datetime,
    us_listed: bool = True,
    tradable: bool = True,
) -> Metrics:
    if len(closes) < 50 or len(highs) < 20:
        raise ValueError(f"{symbol}: need at least 50 closes and 20 highs")
    last = closes[-1]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    high20 = max(highs[-20:])
    roc20 = last / closes[-21] - 1
    return Metrics(
        symbol=symbol,
        instrument_type=instrument_type,
        last_price=last,
        market_cap=market_cap,
        sma20=sma20,
        sma50=sma50,
        high20=high20,
        roc20=roc20,
        as_of=as_of,
        us_listed=us_listed,
        tradable=tradable,
    )


def build_snapshot(raw: dict) -> Snapshot:
    as_of = _parse_time(raw["as_of"])
    metrics: dict[str, Metrics] = {}
    for symbol, row in raw["symbols"].items():
        metrics[symbol] = metrics_from_history(
            symbol=symbol,
            instrument_type=row.get("instrument_type", "stock"),
            market_cap=float(row["market_cap"]),
            closes=[float(x) for x in row["closes"]],
            highs=[float(x) for x in row["highs"]],
            as_of=_parse_time(row.get("as_of", raw["as_of"])),
            us_listed=bool(row.get("us_listed", True)),
            tradable=bool(row.get("tradable", True)),
        )
    return Snapshot(as_of=as_of, metrics=metrics)


def load_raw(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_snapshot(raw: dict, out: Path) -> Snapshot:
    snapshot = build_snapshot(raw)
    payload = {
        "as_of": snapshot.as_of.isoformat(),
        "symbols": {
            symbol: {
                "instrument_type": m.instrument_type,
                "last_price": m.last_price,
                "market_cap": m.market_cap,
                "sma20": round(m.sma20, 6),
                "sma50": round(m.sma50, 6),
                "high20": m.high20,
                "roc20": round(m.roc20, 8),
                "trend_up": m.trend_up,
            }
            for symbol, m in snapshot.metrics.items()
        },
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return snapshot
