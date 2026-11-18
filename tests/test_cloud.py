from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from dashboard import cloud
from dashboard.server import serve

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token-123"

SESSION = {
    "id": "sess_00vu7c733rhfk5p23j38",
    "type": "session",
    "agent": {"id": "agent_00vqvt6kx064gw66ov25", "type": "agent", "version": 7, "name": "moomoo-scout"},
    "environment_id": "env_00vqvntgwlon5klg6q8z",
    "title": "scout-0945 scheduled run",
    "status": "idle",
    "deployment_id": "dep_00vu7529d3ojkzob5pkf",
    "usage": {"model_credits": 1.5, "sandbox_runtime_credits": 0.25, "total_credits": 1.75},
    "created_at": "2026-11-18T14:45:02Z",
    "updated_at": "2026-11-18T14:46:30Z",
}

EVENTS = [
    {"id": "evt_1", "type": "user.message", "content": [{"type": "text", "text": "Run the 09:45 scout cycle."}], "processed_at": "2026-11-18T14:45:03Z"},
    {"id": "evt_2", "type": "span.model_request_start", "processed_at": "2026-11-18T14:45:04Z"},
    {"id": "evt_3", "type": "agent.tool_use", "name": "quote_cur_kline", "input": {"symbol": "US.SPY"}, "processed_at": "2026-11-18T14:45:10Z"},
    {"id": "evt_4", "type": "agent.message", "content": [{"type": "text", "text": "slot 09:45 | due_slot=none | no tickets"}], "processed_at": "2026-11-18T14:45:20Z"},
    {"id": "evt_5", "type": "session.error", "error": {"type": "unknown_error", "message": "boom"}, "processed_at": "2026-11-18T14:45:30Z"},
]


class FakeTransport:
    def __init__(self, respond):
        self.calls = []
        self._respond = respond

    def __call__(self, request, timeout):
        self.calls.append(request)
        return self._respond(request)


def ok(payload: dict):
    return FakeTransport(lambda request: (200, json.dumps(payload).encode("utf-8")))


class EnvTests(unittest.TestCase):
    def test_parses_comments_quotes_and_export(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n\nQODER_PAT_TKN='abc123'\nexport OTHER=\"x=y\"\nEMPTY=\n",
                encoding="utf-8",
            )
            values = cloud.load_env(path)
        self.assertEqual(values["QODER_PAT_TKN"], "abc123")
        self.assertEqual(values["OTHER"], "x=y")
        self.assertEqual(values["EMPTY"], "")

    def test_missing_file_is_empty(self):
        self.assertEqual(cloud.load_env(Path("does-not-exist.env")), {})


class ClientTests(unittest.TestCase):
    def test_sessions_sends_bearer_and_params(self):
        transport = ok({"data": [SESSION], "has_more": False})
        client = cloud.CloudClient(TOKEN, transport=transport)
        rows = client.sessions(limit=5)
        self.assertEqual(len(rows), 1)
        request = transport.calls[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertIn("/api/v1/cloud/sessions?", request.full_url)
        self.assertIn("limit=5", request.full_url)
        self.assertIn("order=desc", request.full_url)

    def test_events_requests_session_path_and_filters(self):
        transport = ok({"data": EVENTS})
        client = cloud.CloudClient(TOKEN, transport=transport)
        client.session_events("sess_abc123", limit=10)
        url = transport.calls[0].full_url
        self.assertIn("/sessions/sess_abc123/events?", url)
        self.assertIn("limit=10", url)

    def test_invalid_session_id_never_reaches_transport(self):
        transport = ok({"data": []})
        client = cloud.CloudClient(TOKEN, transport=transport)
        with self.assertRaises(cloud.CloudError):
            client.session_events("../../agents", limit=5)
        self.assertEqual(transport.calls, [])

    def test_http_error_maps_to_cloud_error(self):
        transport = FakeTransport(
            lambda request: (401, json.dumps({"error": {"type": "authentication_error", "message": "PAT invalid or expired"}}).encode())
        )
        client = cloud.CloudClient(TOKEN, transport=transport)
        with self.assertRaises(cloud.CloudError) as caught:
            client.sessions()
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("PAT invalid", caught.exception.message)

    def test_error_message_redacts_token(self):
        body = json.dumps({"error": {"message": f"bad token {TOKEN}"}}).encode()
        client = cloud.CloudClient(TOKEN, transport=FakeTransport(lambda request: (403, body)))
        with self.assertRaises(cloud.CloudError) as caught:
            client.sessions()
        self.assertNotIn(TOKEN, caught.exception.message)
        self.assertIn("[redacted]", caught.exception.message)

    def test_non_json_body_is_an_error(self):
        client = cloud.CloudClient(TOKEN, transport=FakeTransport(lambda request: (200, b"<html>")))
        with self.assertRaises(cloud.CloudError):
            client.sessions()


class SummarizeTests(unittest.TestCase):
    def test_session_summary(self):
        row = cloud.summarize_session(SESSION)
        self.assertEqual(row["agent"], "moomoo-scout")
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["credits"], 1.75)
        self.assertEqual(row["deployment_id"], "dep_00vu7529d3ojkzob5pkf")

    def test_event_summary_filters_noise_and_formats_tools(self):
        events = cloud.summarize_events(EVENTS)
        types = [e["type"] for e in events]
        self.assertNotIn("span.model_request_start", types)
        tool = next(e for e in events if e["type"] == "agent.tool_use")
        self.assertIn("quote_cur_kline", tool["text"])
        self.assertIn("US.SPY", tool["text"])
        error = next(e for e in events if e["type"] == "session.error")
        self.assertEqual(error["text"], "boom")

    def test_long_text_is_clipped(self):
        event = cloud.summarize_event(
            {"type": "agent.message", "content": [{"type": "text", "text": "x" * 500}]}
        )
        self.assertLessEqual(len(event["text"]), 240)


class CloudServerTests(unittest.TestCase):
    def _serve(self, cloud_client):
        self.httpd = serve(
            runtime=ROOT / "runtime" / "dashboard-demo",
            config_path=ROOT / "config.json",
            baseline=100000.0,
            demo=True,
            cloud=cloud_client,
            port=0,
        )
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        if getattr(self, "httpd", None):
            self.httpd.shutdown()
            self.httpd.server_close()

    def test_sessions_route_keeps_token_server_side(self):
        transport = ok({"data": [SESSION]})
        self._serve(cloud.CloudClient(TOKEN, transport=transport))
        with urlopen(f"http://127.0.0.1:{self.port}/api/cloud/sessions?limit=3", timeout=5) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["sessions"][0]["id"], SESSION["id"])
        self.assertNotIn(TOKEN, raw)
        self.assertEqual(transport.calls[0].get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertIn("limit=3", transport.calls[0].full_url)

    def test_events_route_filters_spans(self):
        self._serve(cloud.CloudClient(TOKEN, transport=ok({"data": EVENTS})))
        with urlopen(
            f"http://127.0.0.1:{self.port}/api/cloud/events?id=sess_abc123&limit=20", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["configured"])
        self.assertEqual(len(payload["events"]), 4)

    def test_events_route_rejects_bad_id(self):
        self._serve(cloud.CloudClient(TOKEN, transport=ok({"data": []})))
        from urllib.error import HTTPError

        with self.assertRaises(HTTPError) as caught:
            urlopen(f"http://127.0.0.1:{self.port}/api/cloud/events?id=../agents", timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_without_pat_reports_disabled(self):
        self._serve(None)
        with urlopen(f"http://127.0.0.1:{self.port}/api/cloud/sessions", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertFalse(payload["configured"])

    def test_upstream_error_is_surfaced_without_token(self):
        self._serve(
            cloud.CloudClient(
                TOKEN,
                transport=FakeTransport(lambda request: (401, b'{"error":{"message":"nope"}}')),
            )
        )
        with urlopen(f"http://127.0.0.1:{self.port}/api/cloud/sessions", timeout=5) as response:
            raw = response.read().decode("utf-8")
        self.assertIn("nope", raw)
        self.assertNotIn(TOKEN, raw)


if __name__ == "__main__":
    unittest.main()
