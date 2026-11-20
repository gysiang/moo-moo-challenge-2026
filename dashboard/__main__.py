from __future__ import annotations

import argparse
import threading
from pathlib import Path

from dashboard.cloud import TOKEN_KEY, CloudClient, load_env
from dashboard.guard import SESSION_KEY, GuardLink
from dashboard.server import serve

DEFAULT_RUNTIME = Path("runtime")
DEFAULT_CONFIG = Path("config.json")
DEFAULT_DEMO_RUNTIME = Path("runtime/dashboard-demo")
DEFAULT_ENV_FILE = Path(".env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dashboard", description="On-demand paper-trading operator dashboard")
    parser.add_argument("--runtime")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--baseline", type=float, default=100000.0)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--no-cloud", action="store_true")
    parser.add_argument("--guard-session")
    parser.add_argument("--no-guard", action="store_true")
    parser.add_argument("--sync-interval", type=float, default=0, help="obsolete; manual requests alone are polled")
    args = parser.parse_args(argv)
    runtime = Path(args.runtime) if args.runtime else (DEFAULT_DEMO_RUNTIME if args.demo else DEFAULT_RUNTIME)
    if not runtime.is_dir():
        parser.error(f"runtime directory not found: {runtime}")
    env = {} if args.no_cloud or args.demo else load_env(Path(args.env_file))
    token = env.get(TOKEN_KEY, "").strip()
    cloud = CloudClient(token) if token and not args.no_cloud and not args.demo else None
    session_id = (args.guard_session or env.get(SESSION_KEY, "")).strip()
    guard = GuardLink(cloud, session_id) if cloud and session_id and not args.no_guard else None
    httpd = serve(runtime=runtime, config_path=Path(args.config), baseline=args.baseline,
                  demo=args.demo, cloud=cloud, guard=guard, port=args.port)
    stopped = threading.Event()

    def poll_requested_actions():
        while not stopped.wait(5):
            try:
                httpd.RequestHandlerClass.app.poll()
            except Exception:
                print("Dashboard result polling failed; no action was restarted.")

    threading.Thread(target=poll_requested_actions, daemon=True, name="requested-action-results").start()
    print(f"dashboard http://127.0.0.1:{args.port} runtime={runtime} demo={args.demo}")
    print("Scout and broker refresh run only on click. No startup research or scheduled worker.")
    print("Guard remains subject to its halt, per-ticket approvals and R1-R11.")
    if args.sync_interval:
        print("--sync-interval ignored: unscoped background artifact sync has been replaced by request-scoped results.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
