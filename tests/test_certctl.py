#!/usr/bin/env python3
"""Focused contract tests for the dependency-free certctl client."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("certctl", ROOT / "scripts" / "certctl.py")
assert SPEC and SPEC.loader
certctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(certctl)


class FakeAPI(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict]] = []
    role = "admin"

    def _json(self, status: int, body: object) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _auth(self) -> bool:
        return self.headers.get("Authorization") == "Bearer test-token"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeAPI.requests.append(("POST", self.path, body))
        if self.path == "/api/auth/login":
            if body.get("username") != "alice" or body.get("password") != "correct horse":
                self._json(401, {"error": "invalid credentials"})
                return
            self._json(200, {"enabled": True, "token": "test-token", "user": {"username": "alice", "role": self.role}})
            return
        if not self._auth():
            self._json(401, {"error": "authentication required"})
            return
        if self.path == "/api/certificates":
            self._json(201, {"id": 7, **body})
            return
        if self.path == "/api/registrations":
            self._json(201, {"id": 3, **body})
            return
        if self.path == "/api/auth/logout":
            self._json(200, {"ok": True})
            return
        if self.path == "/api/agents":
            self._json(201, {"id": 2, "status": "active", **body})
            return
        if self.path == "/api/users":
            self._json(201, {"id": 4, "status": "active", **body})
            return
        if self.path == "/api/agents/2/users":
            self._json(201, {"id": 5, "status": "active", "agent_id": 2, **body})
            return
        self._json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        FakeAPI.requests.append(("GET", self.path, {}))
        if not self._auth():
            self._json(401, {"error": "authentication required"})
            return
        path = urlsplit(self.path).path
        if path == "/api/auth/me":
            self._json(200, {"enabled": True, "user": {"username": "alice", "role": self.role}})
        elif path == "/api/certificates":
            self._json(200, {"items": [{"id": 7, "name": "one.test"}], "total": 1})
        elif path == "/api/certificates/7":
            self._json(200, {"id": 7, "name": "one.test", "files": ["rsa_one.test.crt"]})
        elif path == "/api/registrations":
            self._json(200, {"items": [], "total": 0})
        elif path == "/api/agents":
            self._json(200, {"items": [{"id": 2, "name": "agent-a", "status": "active"}], "total": 1})
        elif path == "/api/agents/2":
            self._json(200, {"id": 2, "name": "agent-a", "status": "active"})
        elif path == "/api/agents/2/users":
            self._json(200, {"items": [{"id": 5, "username": "agent-user", "agent_id": 2}], "total": 1})
        elif path == "/api/users":
            self._json(200, {"items": [{"id": 4, "username": "admin-user", "role": "admin"}], "total": 1})
        elif path == "/api/users/4":
            self._json(200, {"id": 4, "username": "admin-user", "role": "admin"})
        elif path == "/api/certificates/7/download":
            body = b"PK\x03\x04fake zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="server-one.test-7.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeAPI.requests.append(("PATCH", self.path, body))
        if not self._auth():
            self._json(401, {"error": "authentication required"})
            return
        self._json(200, {"ok": True, **body})

    def log_message(self, *_args: object) -> None:
        pass


class CertctlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeAPI.requests.clear()
        FakeAPI.role = "admin"
        self.session_tmp = tempfile.TemporaryDirectory()
        self.env = {
            "CERTCTL_URL": self.url,
            "CERTCTL_USERNAME": "alice",
            "CERTCTL_PASSWORD": "correct horse",
            "CERTCTL_SESSION_FILE": str(Path(self.session_tmp.name) / "session.json"),
        }
        self.old_env = os.environ.copy()
        os.environ.update(self.env)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.session_tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = certctl.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_login_uses_bearer_and_does_not_print_password_or_token(self) -> None:
        code, output, error = self.run_cli("--json", "login")
        self.assertEqual(code, 0)
        self.assertIn('"username": "alice"', output)
        self.assertNotIn("correct horse", output + error)
        self.assertNotIn("test-token", output + error)

    def test_login_token_is_cached_without_password_and_reused(self) -> None:
        code, _output, _error = self.run_cli("login")
        self.assertEqual(code, 0)
        session = Path(os.environ["CERTCTL_SESSION_FILE"])
        self.assertTrue(session.is_file())
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)
        os.environ.pop("CERTCTL_USERNAME")
        os.environ.pop("CERTCTL_PASSWORD")
        code, output, _error = self.run_cli("--json", "list")
        self.assertEqual(code, 0)
        self.assertIn('"total": 1', output)
        self.assertTrue(any(method == "GET" and path == "/api/certificates" for method, path, _body in FakeAPI.requests))

    def test_admin_can_assign_agent_and_list(self) -> None:
        code, output, _ = self.run_cli("--json", "issue", "one.test", "--agent", "agent-a")
        self.assertEqual(code, 0)
        posted = next(body for method, path, body in FakeAPI.requests if method == "POST" and path == "/api/certificates")
        self.assertEqual(posted["agent_id"], 2)
        self.assertIn('"id": 7', output)

        code, output, _ = self.run_cli("--json", "list", "--agent", "agent-a")
        self.assertEqual(code, 0)
        queried = next(path for method, path, _ in FakeAPI.requests if method == "GET" and path.startswith("/api/certificates?"))
        self.assertEqual(parse_qs(urlsplit(queried).query)["agent_id"], ["2"])
        self.assertIn('"total": 1', output)

    def test_agent_cannot_use_agent_override(self) -> None:
        FakeAPI.role = "agent"
        code, _output, error = self.run_cli("--json", "issue", "one.test", "--agent", "agent-b")
        self.assertEqual(code, certctl.EXIT_FORBIDDEN)
        self.assertIn("only available to administrators", error)
        self.assertFalse(any(method == "POST" and path == "/api/certificates" for method, path, _ in FakeAPI.requests))

    def test_download_writes_file_and_returns_json_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bundle.zip"
            code, output, _ = self.run_cli("--json", "download", "7", "-o", str(target))
            self.assertEqual(code, 0)
            self.assertEqual(target.read_bytes(), b"PK\x03\x04fake zip")
            self.assertIn('"bytes": 12', output)

    def test_admin_agent_lifecycle_commands(self) -> None:
        code, output, _ = self.run_cli("--json", "agents", "create", "agent-b")
        self.assertEqual(code, 0)
        self.assertIn('"name": "agent-b"', output)
        self.assertTrue(any(method == "POST" and path == "/api/agents" for method, path, _ in FakeAPI.requests))

        code, output, _ = self.run_cli("--json", "agents", "disable", "agent-a")
        self.assertEqual(code, 0)
        method, path, body = next((item for item in FakeAPI.requests if item[0] == "PATCH" and item[1] == "/api/agents/2"), (None, None, None))
        self.assertEqual(body, {"status": "disabled"})

    def test_admin_user_create_reads_password_from_environment_and_reset(self) -> None:
        os.environ["CERTCTL_NEW_PASSWORD"] = "new secure password"
        code, output, _ = self.run_cli("--json", "users", "create", "new-agent", "--agent", "agent-a")
        self.assertEqual(code, 0)
        self.assertNotIn("new secure password", output)
        posted = next(body for method, path, body in FakeAPI.requests if method == "POST" and path == "/api/agents/2/users")
        self.assertEqual(posted["password"], "new secure password")

        code, output, _ = self.run_cli("--json", "users", "reset-password", "4")
        self.assertEqual(code, 0)
        patched = next(body for method, path, body in FakeAPI.requests if method == "PATCH" and path == "/api/users/4")
        self.assertEqual(patched["password"], "new secure password")
        self.assertNotIn("new secure password", output)

    def test_non_admin_is_rejected_before_management_request(self) -> None:
        FakeAPI.role = "agent"
        code, _output, error = self.run_cli("--json", "agents", "list")
        self.assertEqual(code, certctl.EXIT_FORBIDDEN)
        self.assertIn("administrator role required", error)
        self.assertFalse(any(path == "/api/agents" for _method, path, _body in FakeAPI.requests))


if __name__ == "__main__":
    unittest.main()
