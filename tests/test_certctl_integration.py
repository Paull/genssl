#!/usr/bin/env python3
"""CLI against a real in-process API with two isolated agent accounts."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from server import create_server


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("certctl_integration_module", ROOT / "scripts" / "certctl.py")
assert SPEC and SPEC.loader
certctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(certctl)


class RealAPICLIIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.old_anonymous = os.environ.pop("CERT_ALLOW_ANONYMOUS", None)
        cls.server = create_server("127.0.0.1", 0, root, root / "out" / "certificates.db", web_username="bootstrap", web_password="bootstrap password")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.store.close()
        cls.tmp.cleanup()
        if cls.old_anonymous is not None:
            os.environ["CERT_ALLOW_ANONYMOUS"] = cls.old_anonymous

    def setUp(self) -> None:
        self.old_env = os.environ.copy()
        self.sessions = tempfile.TemporaryDirectory()
        os.environ.update({"CERTCTL_URL": self.url, "CERTCTL_SESSION_FILE": str(Path(self.sessions.name) / "session.json")})
        for name in ("CERTCTL_TOKEN", "API_TOKEN", "CERTCTL_NEW_PASSWORD", "CERTCTL_USER_PASSWORD"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.sessions.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, object, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = certctl.main(["--json", *argv])
        value: object = None
        if out.getvalue().strip():
            value = json.loads(out.getvalue())
        return code, value, err.getvalue()

    def use_user(self, username: str, password: str) -> None:
        os.environ["CERTCTL_USERNAME"] = username
        os.environ["CERTCTL_PASSWORD"] = password
        os.environ.pop("CERTCTL_TOKEN", None)

    def test_two_agent_cli_workflows_are_scoped(self) -> None:
        self.use_user("bootstrap", "bootstrap password")
        code, alpha, error = self.run_cli("agents", "create", "alpha")
        self.assertEqual(code, 0, error)
        code, beta, error = self.run_cli("agents", "create", "beta")
        self.assertEqual(code, 0, error)
        alpha_id, beta_id = int(alpha["id"]), int(beta["id"])

        os.environ["CERTCTL_NEW_PASSWORD"] = "alpha secure password"
        code, alpha_user, error = self.run_cli("users", "create", "alice", "--agent", "alpha")
        self.assertEqual(code, 0, error)
        os.environ["CERTCTL_NEW_PASSWORD"] = "beta secure password"
        code, beta_user, error = self.run_cli("users", "create", "bob", "--agent", "beta")
        self.assertEqual(code, 0, error)
        self.assertEqual(alpha_user["agent_id"], alpha_id)
        self.assertEqual(beta_user["agent_id"], beta_id)

        self.use_user("alice", "alpha secure password")
        code, _value, error = self.run_cli("login")
        self.assertEqual(code, 0, error)
        code, _value, error = self.run_cli("registration-create", "alpha-app")
        self.assertEqual(code, 0, error)
        code, alpha_registrations, error = self.run_cli("registration-list")
        self.assertEqual(code, 0, error)
        self.assertEqual([item["name"] for item in alpha_registrations["items"]], ["alpha-app"])

        self.use_user("bob", "beta secure password")
        code, _value, error = self.run_cli("registration-create", "beta-app")
        self.assertEqual(code, 0, error)
        code, beta_registrations, error = self.run_cli("registration-list")
        self.assertEqual(code, 0, error)
        self.assertEqual([item["name"] for item in beta_registrations["items"]], ["beta-app"])

        # A regular agent cannot use the administrator ownership override.
        code, _value, error = self.run_cli("registration-create", "cross-app", "--agent", str(alpha_id))
        self.assertEqual(code, certctl.EXIT_FORBIDDEN)
        self.assertIn("administrators", error)

        # Disable one identity through the admin CLI; subsequent login fails.
        self.use_user("bootstrap", "bootstrap password")
        code, _value, error = self.run_cli("users", "disable", str(alpha_user["id"]))
        self.assertEqual(code, 0, error)
        self.use_user("alice", "alpha secure password")
        code, _value, _error = self.run_cli("login")
        self.assertEqual(code, certctl.EXIT_AUTH)


if __name__ == "__main__":
    unittest.main()
