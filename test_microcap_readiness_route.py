import asyncio
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.security import HTTPBasic
from microcap_readiness import _repository_roots

try:
    import httpx
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


def isolated_server():
    core = types.ModuleType("signal_server_core")
    core.app = FastAPI()
    security = HTTPBasic()

    def dashboard_auth(credentials=Depends(security)):
        return credentials.username

    core.dashboard_auth = dashboard_auth
    core.__all__ = ["app", "dashboard_auth"]
    spec = importlib.util.spec_from_file_location(
        "isolated_signal_server_route", Path(__file__).with_name("signal_server.py")
    )
    server = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"signal_server_core": core}):
        spec.loader.exec_module(server)
    return server, dashboard_auth


def request(app, path, *, auth=False):
    headers = [(b"authorization", b"Basic dXNlcjpwYXNz")] if auth else []
    if TestClient is not None:
        with TestClient(app) as client:
            response = client.get(path, headers=dict(headers))
        return response.status_code, response.json()

    async def run():
        replies = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            replies.append(message)

        await app({
            "type": "http", "http_version": "1.1", "method": "GET", "path": path,
            "raw_path": path.encode(), "query_string": b"", "headers": headers,
            "scheme": "http", "server": ("test", 80), "client": ("test", 12345),
        }, receive, send)
        start = next(item for item in replies if item["type"] == "http.response.start")
        body = b"".join(item["body"] for item in replies
                        if item["type"] == "http.response.body")
        return start["status"], json.loads(body)

    return asyncio.run(run())


def request_raw(app, path, *, auth=False):
    headers = [(b"authorization", b"Basic dXNlcjpwYXNz")] if auth else []
    if TestClient is not None:
        with TestClient(app) as client:
            response = client.get(path, headers=dict(headers))
        return response.status_code, response.text, response.headers.get("content-type", "")

    async def run():
        replies = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            replies.append(message)

        await app({
            "type": "http", "http_version": "1.1", "method": "GET", "path": path,
            "raw_path": path.encode(), "query_string": b"", "headers": headers,
            "scheme": "http", "server": ("test", 80), "client": ("test", 12345),
        }, receive, send)
        start = next(item for item in replies if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in replies
                        if item["type"] == "http.response.body")
        content_type = dict(start.get("headers", [])).get(b"content-type", b"").decode()
        return start["status"], body.decode(), content_type

    return asyncio.run(run())


class ReadinessRouteTests(unittest.TestCase):
    def setUp(self):
        self.server, self.auth = isolated_server()

    def test_route_requires_existing_dashboard_auth(self):
        route = next(route for route in self.server.app.routes
                     if getattr(route, "path", None) == "/microcap-research-status")
        self.assertIn(self.auth, [dependency.call for dependency in route.dependant.dependencies])
        status, _ = request(self.server.app, route.path)
        self.assertEqual(status, 401)

    def test_authenticated_route_returns_sanitized_missing_snapshot(self):
        self.server.app.dependency_overrides[self.auth] = lambda: "authorized"
        with patch.dict("os.environ", {"MICROCAP_READINESS_PATH": ""}):
            status, body = request(self.server.app, "/microcap-research-status")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "UNAVAILABLE")
        self.assertEqual(body["decision"], "NO_TRADE")

    def test_malformed_snapshot_does_not_affect_strategy_status_route(self):
        self.server.app.dependency_overrides[self.auth] = lambda: "authorized"
        roots = _repository_roots()
        with patch.dict("os.environ", {"MICROCAP_READINESS_PATH": "/etc/hosts"}), \
                patch("microcap_readiness._repository_roots", return_value=roots), \
                patch("pathlib.Path.open", return_value=io.BytesIO(b'{"malformed":')):
            status, body = request(self.server.app, "/microcap-research-status")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "UNAVAILABLE")
        paths = {route.path for route in self.server.app.routes}
        self.assertIn("/strategy-status", paths)
        with patch.object(self.server, "read_snapshot", return_value={"heartbeat_epoch": None}):
            status, original = request(self.server.app, "/strategy-status")
        self.assertEqual(status, 200)
        self.assertEqual(original, {"heartbeat_epoch": None, "age_seconds": None,
                                    "fresh": False})

    def test_microcap_panel_script_requires_auth_and_serves_javascript(self):
        status, _, _ = request_raw(self.server.app, "/microcap-research-panel.js")
        self.assertEqual(status, 401)
        self.server.app.dependency_overrides[self.auth] = lambda: "authorized"
        status, body, content_type = request_raw(
            self.server.app, "/microcap-research-panel.js", auth=True
        )
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", content_type)
        self.assertIn("renderMicrocapResearch", body)
        self.assertIn("loadMicrocapResearch", body)

    def test_dashboard_legacy_and_app_script_require_auth(self):
        server, _ = isolated_server()
        for path, media in (("/dashboard-legacy", "text/html"),
                            ("/dashboard-app.js", "application/javascript")):
            with self.subTest(path=path):
                status, _, _ = request_raw(server.app, path)
                self.assertEqual(status, 401)
                status, body, content_type = request_raw(server.app, path, auth=True)
                self.assertEqual(status, 200)
                self.assertTrue(content_type.startswith(media))
                self.assertTrue(body.strip())

    def test_query_parameter_cannot_choose_snapshot_path(self):
        self.server.app.dependency_overrides[self.auth] = lambda: "authorized"
        route = next(route for route in self.server.app.routes
                     if getattr(route, "path", None) == "/microcap-research-status")
        self.assertEqual([param.name for param in route.dependant.query_params], [])


if __name__ == "__main__":
    unittest.main()
