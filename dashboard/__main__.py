from __future__ import annotations

import argparse
from pathlib import Path

from dashboard.cloud import TOKEN_KEY, CloudClient, load_env
from dashboard.guard import SESSION_KEY, GuardLink
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
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help=f"file holding {TOKEN_KEY} and {SESSION_KEY}",
    )
    parser.add_argument("--no-cloud", action="store_true", help="disable Cloud Agents access")
    parser.add_argument("--guard-session", help=f"Guard Forward session id (default: {SESSION_KEY})")
    parser.add_argument("--no-guard", action="store_true", help="approve without starting the Guard")
    args = parser.parse_args(argv)

    runtime = Path(args.runtime) if args.runtime else (DEFAULT_DEMO_RUNTIME if args.demo else DEFAULT_RUNTIME)
    if not runtime.is_dir():
        parser.error(f"runtime directory not found: {runtime}")

    cloud = None
    guard = None
    env_file = Path(args.env_file)
    env = load_env(env_file) if env_file.exists() else {}
    token = env.get(TOKEN_KEY, "").strip()
    if not args.no_cloud and token:
        cloud = CloudClient(token)
    session_id = (args.guard_session or env.get(SESSION_KEY, "")).strip()
    if not args.no_guard and cloud is not None and session_id:
        try:
            guard = GuardLink(cloud, session_id)
        except ValueError as err:
            parser.error(str(err))
    if args.demo and guard is not None:
        guard = None
        print("guard link: disabled in demo mode (synthetic fixtures never place orders)")

    httpd = serve(
        runtime=runtime,
        config_path=Path(args.config),
        baseline=args.baseline,
        demo=args.demo,
        cloud=cloud,
        guard=guard,
        port=args.port,
    )
    print(f"dashboard http://127.0.0.1:{args.port}  runtime={runtime}  demo={args.demo}")
    if cloud is not None:
        print(f"cloud agents: enabled via {TOKEN_KEY} in {env_file} (server-side only)")
    else:
        print(f"cloud agents: disabled (no {TOKEN_KEY} in {env_file}, or --no-cloud)")
    if guard is not None:
        print(f"guard link: approve starts Forward session {session_id}")
    else:
        print(f"guard link: disabled (no {SESSION_KEY} in {env_file}, or --no-guard)")
    print("approvals write to", runtime / "approvals.json")
    print("rejections append to", runtime / "rejections.jsonl")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
