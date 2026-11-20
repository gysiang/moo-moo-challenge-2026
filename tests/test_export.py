from __future__ import annotations

import json
import shutil
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from dashboard.export import export_site

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "dashboard" / "static"
DEMO_RUNTIME = ROOT / "runtime" / "dashboard-demo"

CLOUD_ROW = {
    "id": "sess_00vu7c733rhfk5p23j38",
    "type": "session",
    "agent": {"id": "agent_1", "name": "moomoo-scout"},
    "status": "idle",
    "title": "scout-0945 scheduled run",
    "deployment_id": "dep_1",
    "usage": {"total_credits": 1.75},
    "created_at": "2026-11-18T14:45:02Z",
    "updated_at": "2026-11-18T14:46:30Z",
}


class FakeCloud:
    def __init__(self, rows=None):
        self._rows = rows if rows is not None else [CLOUD_ROW]

    def sessions(self, *, limit=12):
        return self._rows


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        shutil.copytree(DEMO_RUNTIME, self.runtime)

    def export(self, cloud_client=None, demo=True, name="web"):
        out = self.root / name
        summary = export_site(
            runtime=self.runtime,
            config_path=ROOT / "config.json",
            baseline=100000.0,
            out_dir=out,
            demo=demo,
            cloud_client=cloud_client,
        )
        return out, summary

    def test_bundle_has_required_files(self):
        out, summary = self.export(cloud_client=FakeCloud())
        for name in ("index.html", "app.js", "style.css", "state.json", "cloud.json"):
            self.assertTrue((out / name).is_file(), name)
        self.assertGreater(summary["bytes"], 0)

    def test_index_is_static_labelled_with_relative_assets(self):
        out, _ = self.export()
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-static="1"', html)
        self.assertIn('href="style.css"', html)
        self.assertIn('src="app.js"', html)
        self.assertNotIn("/static/", html)
        self.assertIn("read-only snapshot", html.lower())

    def test_app_gates_write_actions_in_static_mode(self):
        out, _ = self.export()
        js = (out / "app.js").read_text(encoding="utf-8")
        self.assertEqual(js, (STATIC_DIR / "app.js").read_text(encoding="utf-8"))
        self.assertIn('document.documentElement.dataset.static === "1"', js)
        self.assertIn("!STATIC_MODE && d.status === \"AWAITING_APPROVAL\"", js)
        self.assertIn("if (STATIC_MODE) {", js)
        self.assertNotIn("setInterval(", js.split("if (!STATIC_MODE) {", 1)[0])

    def test_hosted_scout_bundle_keeps_trading_static_and_includes_controller(self):
        out = self.root / "hosted"
        export_site(runtime=self.runtime, config_path=ROOT / "config.json", baseline=100000,
                    out_dir=out, demo=True, hosted_scout=True)
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-static="1" data-hosted-scout="1"', html)
        self.assertIn('src="hosted-scout.js"', html)
        self.assertIn("trading", html.lower())
        self.assertTrue((out / "hosted-scout.js").is_file())
        self.assertIn("Research is not an approved ticket", html)
        self.assertIn('id="scout-report"', html)

    def test_plain_static_export_does_not_connect_hosted_scout(self):
        out, _ = self.export()
        self.assertNotIn('data-hosted-scout="1"', (out / "index.html").read_text(encoding="utf-8"))
        self.assertFalse((out / "hosted-scout.js").exists())

    def test_state_snapshot_is_demo_and_complete(self):
        out, _ = self.export()
        state = json.loads((out / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["demo"])
        self.assertTrue(state["has_snapshot"])
        self.assertIn("kpi", state)
        self.assertIn("decisions", state)
        self.assertIn("positions", state)

    def test_bundle_carries_market_session_and_eastern_clock(self):
        out, _ = self.export()
        state = json.loads((out / "state.json").read_text(encoding="utf-8"))
        self.assertIn(state["market"]["state"], {"OPEN", "CLOSED"})
        self.assertEqual(state["market"]["session_open"], "09:30")
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="session"', html)
        js = (out / "app.js").read_text(encoding="utf-8")
        self.assertIn('const ET_ZONE = "America/New_York"', js)
        self.assertIn("renderSession(s);", js)
        css = (out / "style.css").read_text(encoding="utf-8")
        self.assertIn('.session[data-state="open"]', css)
        self.assertIn('.session[data-state="closed"]', css)

    def test_cloud_snapshot_lists_sessions_without_events(self):
        out, summary = self.export(cloud_client=FakeCloud())
        payload = json.loads((out / "cloud.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["sessions"][0]["agent"], "moomoo-scout")
        self.assertNotIn("events", payload["sessions"][0])
        self.assertEqual(summary["sessions"], 1)

    def test_no_cloud_snapshot_when_client_absent(self):
        out, _ = self.export(cloud_client=None)
        payload = json.loads((out / "cloud.json").read_text(encoding="utf-8"))
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["sessions"], [])

    def test_bundle_contains_no_credentials_or_env_values(self):
        token = "pat-SECRET-9f3c"
        (self.root / ".env").write_text(f"QODER_PAT_TKN={token}\n", encoding="utf-8")
        out, _ = self.export()
        for path in out.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(token, text, path.name)
            self.assertNotIn("QODER_PAT_TKN", text, path.name)
            self.assertNotIn("Authorization", text, path.name)

    def test_export_does_not_touch_runtime_history(self):
        history = self.runtime / "equity-history-demo.jsonl"
        before = history.read_bytes()
        self.export()
        self.assertEqual(history.read_bytes(), before)

    def test_static_bundle_serves_over_http(self):
        out, _ = self.export(cloud_client=FakeCloud())
        handler = partial(SimpleHTTPRequestHandler, directory=str(out))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]

        with urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5) as response:
            self.assertEqual(response.status, 200)
            html = response.read().decode("utf-8")
        self.assertIn("Moomoo", html)

        with urlopen(f"http://127.0.0.1:{port}/state.json", timeout=5) as response:
            state = json.loads(response.read().decode("utf-8"))
        self.assertIn("kpi", state)
        self.assertEqual(state["account_id"], "5829804")

        with urlopen(f"http://127.0.0.1:{port}/cloud.json", timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["configured"])


if __name__ == "__main__":
    unittest.main()
