from __future__ import annotations

import argparse
from pathlib import Path

from dashboard.cloud import TOKEN_KEY, CloudClient, load_env
from dashboard.server import serve

DEFAULT_RUNTIME = Path("runtime")
DEFAULT_CONFIG = Path("config.json")
DEFAULT_DEMO_RUNTIME = Path("runtime/dashboard-demo")
DEFAULT_ENV_FILE = Path(".env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dashboard",
        description="Moomoo paper-trading operator dashboard (read-only + approvals).",
    )
    parser.add_argument("--runtime", help="directory holding account/cycle/event artefacts")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--demo", action="store_true", help="serve the labelled demo fixtures")
    parser.add_argument("--baseline", type=float, default=100000.0, help="starting equity")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="file holding QODER_PAT_TKN")
    parser.add_argument("--no-cloud", action="store_true", help="disable Cloud Agents session reads")
    args = parser.parse_args(argv)

    runtime = Path(args.runtime) if args.runtime else (DEFAULT_DEMO_RUNTIME if args.demo else DEFAULT_RUNTIME)
    if not runtime.is_dir():
        parser.error(f"runtime directory not found: {runtime}")

    cloud = None
    env_file = Path(args.env_file)
    if not args.no_cloud and env_file.exists():
        token = load_env(env_file).get(TOKEN_KEY, "").strip()
        if token:
            cloud = CloudClient(token)

    httpd = serve(
        runtime=runtime,
        config_path=Path(args.config),
        baseline=args.baseline,
        demo=args.demo,
        cloud=cloud,
        port=args.port,
    )
    print(f"dashboard http://127.0.0.1:{args.port}  runtime={runtime}  demo={args.demo}")
    if cloud is not None:
        print(f"cloud agents: reads enabled via {TOKEN_KEY} in {env_file} (server-side only)")
    else:
        print(f"cloud agents: reads disabled (no {TOKEN_KEY} in {env_file}, or --no-cloud)")
    print("approvals write to", runtime / "approvals.json", "- orders are placed only by the Guard")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
