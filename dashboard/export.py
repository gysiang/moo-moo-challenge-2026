"""Render the operator dashboard as a read-only static bundle for publishing."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dashboard import cloud as cloud_api
from dashboard import state as state_api

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_OUT = Path("web")

FOOTER = """  <footer>
    Read-only snapshot of the paper-account operator dashboard. Approvals and Guard runs happen
    only in the local dashboard; this copy has no broker access and no write endpoints.
  </footer>"""


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _index_html() -> str:
    html = _read("index.html")
    html = html.replace('<html lang="en">', '<html lang="en" data-static="1">', 1)
    html = html.replace("/static/style.css", "style.css")
    html = html.replace("/static/app.js", "app.js")
    start = html.index("  <footer>")
    end = html.index("</footer>") + len("</footer>")
    return html[:start] + FOOTER.strip() + html[end:]


def build_state(runtime: Path, config_path: Path, baseline: float, demo: bool) -> dict:
    side_effects_dir = Path(tempfile.mkdtemp(prefix="dashboard-export-"))
    try:
        for name in ("equity-history.jsonl", "equity-history-demo.jsonl"):
            source = runtime / name
            if source.exists():
                shutil.copyfile(source, side_effects_dir / name)
        return state_api.build_state(
            runtime=runtime,
            config_path=config_path,
            baseline=baseline,
            demo=demo,
            approvals_file="approvals-demo.json" if demo else "approvals.json",
            events_file="guard-events-demo.jsonl" if demo else "guard-events.jsonl",
            history_file=str(side_effects_dir / ("equity-history-demo.jsonl" if demo else "equity-history.jsonl")),
            rejections_file="rejections-demo.jsonl" if demo else "rejections.jsonl",
            triggers_file="guard-triggers-demo.jsonl" if demo else "guard-triggers.jsonl",
        )
    finally:
        shutil.rmtree(side_effects_dir, ignore_errors=True)


def cloud_snapshot(client: Optional[cloud_api.CloudClient], limit: int = 12) -> dict:
    if client is None:
        return {"configured": False, "sessions": []}
    try:
        rows = client.sessions(limit=limit)
    except cloud_api.CloudError as err:
        return {"configured": True, "error": err.message, "sessions": []}
    return {"configured": True, "sessions": [cloud_api.summarize_session(r) for r in rows]}


def export_site(
    *,
    runtime: Path,
    config_path: Path,
    baseline: float,
    out_dir: Path,
    demo: bool = False,
    cloud_client: Optional[cloud_api.CloudClient] = None,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    payload = build_state(Path(runtime), Path(config_path), baseline, demo)
    for decision in payload.get("decisions") or []:
        decision.pop("guard_session_id", None)
    payload.pop("guard_link", None)
    cloud_payload = cloud_snapshot(cloud_client)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(_index_html(), encoding="utf-8")
    (out / "app.js").write_text(_read("app.js"), encoding="utf-8")
    (out / "style.css").write_text(_read("style.css"), encoding="utf-8")
    state_text = json.dumps(payload, indent=1, sort_keys=True)
    (out / "state.json").write_text(state_text + "\n", encoding="utf-8")
    (out / "cloud.json").write_text(
        json.dumps(cloud_payload, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "out": str(out),
        "as_of": payload.get("as_of"),
        "demo": payload.get("demo"),
        "decisions": len(payload.get("decisions") or []),
        "positions": len(payload.get("positions") or []),
        "sessions": len(cloud_payload.get("sessions") or []),
        "bytes": sum(p.stat().st_size for p in out.iterdir() if p.is_file()),
        "exported_at": now.isoformat(),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.export", description="Build the read-only static dashboard bundle.")
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--baseline", type=float, default=100000.0)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-cloud", action="store_true", help="omit the cloud session snapshot")
    args = parser.parse_args(argv)

    runtime = Path(args.runtime)
    if not runtime.is_dir():
        print(f"runtime directory not found: {runtime}", file=sys.stderr)
        return 2

    client = None
    if not args.no_cloud:
        token = cloud_api.load_env(Path(args.env_file)).get(cloud_api.TOKEN_KEY, "").strip()
        if token:
            client = cloud_api.CloudClient(token)

    summary = export_site(
        runtime=runtime,
        config_path=Path(args.config),
        baseline=args.baseline,
        out_dir=Path(args.out),
        demo=args.demo,
        cloud_client=client,
    )
    print(
        f"exported {summary['bytes']} bytes to {summary['out']} · as of {summary['as_of']} · "
        f"{summary['decisions']} decisions · {summary['positions']} positions · "
        f"{summary['sessions']} cloud sessions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
