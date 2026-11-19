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


def ema(closes: list[float], period: int) -> float:
    if period < 1:
        raise ValueError(f"EMA period must be positive, got {period}")
    if len(closes) < period:
        raise ValueError(f"need at least {period} closes for EMA{period}")
    value = sum(closes[:period]) / period
    weight = 2 / (period + 1)
    for close in closes[period:]:
        value = close * weight + value * (1 - weight)
    return value


def metrics_from_history(
    symbol: str,
    instrument_type: str,
    market_cap: float,
    closes: list[float],
    highs: list[float],
    as_of: datetime,
    us_listed: bool = True,
    tradable: bool = True,
    ema_fast: int = 8,
    ema_slow: int = 21,
) -> Metrics:
    if len(closes) < 50 or len(highs) < 20:
        raise ValueError(f"{symbol}: need at least 50 closes and 20 highs")
    if ema_fast >= ema_slow:
        raise ValueError(f"{symbol}: fast EMA period must be below slow period")
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
        ema_fast=ema(closes, ema_fast),
        ema_slow=ema(closes, ema_slow),
        high20=high20,
        roc20=roc20,
        as_of=as_of,
        us_listed=us_listed,
        tradable=tradable,
    )


def build_snapshot(raw: dict, ema_fast: int = 8, ema_slow: int = 21) -> Snapshot:
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
            ema_fast=ema_fast,
            ema_slow=ema_slow,
        )
    return Snapshot(as_of=as_of, metrics=metrics)


def load_raw(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_snapshot(
    raw: dict, out: Path, ema_fast: int = 8, ema_slow: int = 21
) -> Snapshot:
    snapshot = build_snapshot(raw, ema_fast=ema_fast, ema_slow=ema_slow)
    payload = {
        "as_of": snapshot.as_of.isoformat(),
        "ema_fast_period": ema_fast,
        "ema_slow_period": ema_slow,
        "symbols": {
            symbol: {
                "instrument_type": m.instrument_type,
                "last_price": m.last_price,
                "market_cap": m.market_cap,
                "sma20": round(m.sma20, 6),
                "sma50": round(m.sma50, 6),
                "ema_fast": round(m.ema_fast, 6),
                "ema_slow": round(m.ema_slow, 6),
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
