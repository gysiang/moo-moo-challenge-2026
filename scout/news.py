from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

MAX_ITEMS = 3
DEFAULT_MAX_AGE_HOURS = 24.0
CLOCK_SKEW = timedelta(seconds=300)
EPOCH_MS_FLOOR = 100_000_000_000
TITLE_LIMIT = 160


def parse_moment(value: Any) -> Optional[datetime]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number >= EPOCH_MS_FLOOR:
            number /= 1000.0
        return datetime.fromtimestamp(number, timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_moment(int(text))
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    return None


def raw_items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            for key in ("items", "news_list", "news"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
            return []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _as_of(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def normalize_item(
    row: dict, now: datetime, max_age: timedelta
) -> Optional[dict]:
    ident = row.get("news_id") or row.get("id")
    if ident in (None, ""):
        return None
    published = parse_moment(row.get("publish_time", row.get("publishTime")))
    if published is None:
        return None
    if (now - published) > max_age:
        return None
    if (published - now) > CLOCK_SKEW:
        return None
    title = " ".join(str(row.get("title") or row.get("abstract") or "").split())
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 1] + "…"
    source = str(row.get("source") or "").strip() or "moomoo.news_search"
    return {"id": f"N-{ident}", "as_of": _as_of(published), "source": source, "title": title}


def normalize(
    raw: dict[str, Any],
    *,
    now: datetime,
    max_age: timedelta = timedelta(hours=DEFAULT_MAX_AGE_HOURS),
    limit: int = MAX_ITEMS,
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """Map raw news_search replies to planner evidence, newest first.

    Items older than max_age (the gate horizon) or stamped in the future are
    dropped: citing them only produces R10_EVIDENCE_STALE.
    """
    if not isinstance(raw, dict):
        raise ValueError("raw news file must be a JSON object keyed by symbol")
    evidence: dict[str, list[dict]] = {}
    report: dict[str, dict[str, int]] = {}
    for symbol, payload in raw.items():
        rows = raw_items(payload)
        items = [item for item in (normalize_item(row, now, max_age) for row in rows) if item]
        items.sort(key=lambda item: item["as_of"], reverse=True)
        kept = items[: max(1, int(limit))]
        evidence[symbol] = kept
        report[symbol] = {"rows": len(rows), "kept": len(kept), "dropped": len(rows) - len(kept)}
    return evidence, report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scout.news", description="Normalize news_search replies into planner evidence"
    )
    parser.add_argument("--in", dest="source", required=True, help="raw news_search JSON")
    parser.add_argument("--out", required=True, help="evidence JSON for scout plan --news")
    parser.add_argument("--now", help="ISO-8601 evaluation time; defaults to now")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--limit", type=int, default=MAX_ITEMS)
    args = parser.parse_args(argv)

    now = parse_moment(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print(f"invalid --now: {args.now}")
        return 2
    if args.limit < 1 or args.limit > 10:
        print("--limit must be between 1 and 10")
        return 2
    raw = json.loads(Path(args.source).read_text(encoding="utf-8"))
    evidence, report = normalize(
        raw,
        now=now,
        max_age=timedelta(hours=max(0.0, args.max_age_hours)),
        limit=args.limit,
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    kept = sum(row["kept"] for row in report.values())
    print(f"{len(evidence)} symbols, {kept} items -> {args.out}")
    for symbol in sorted(report):
        row = report[symbol]
        print(f"  {symbol:<9} kept {row['kept']}/{args.limit} dropped {row['dropped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
