#!/usr/bin/env python3
"""Regression tests for the five post-v3 review findings."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from server import create_server


ROOT = Path(__file__).resolve().parents[1]


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_environment = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix="genssl-review-")
        self.root = Path(self.tmp.name)
        for filename in ("server.py", "ca.cnf", "gen_root_cert.sh", "gen_server_cert.sh", "gen_client_cert.sh"):
            shutil.copy(ROOT / filename, self.root / filename)
        shutil.copytree(ROOT / "scripts", self.root / "scripts")
        self.env = {**os.environ, "ROOTPASS": "review-root-password", "CERT_ROOT_DIR": str(self.root), "CERT_DB": str(self.root / "out" / "certificates.db")}
        os.environ["ROOTPASS"] = "review-root-password"
        os.environ["CERT_ALLOW_ANONYMOUS"] = ""
        self.server = create_server("127.0.0.1", 0, self.root, self.root / "out" / "certificates.db", web_username="review-admin", web_password="review-admin-password")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.store.close()
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.old_environment)

    def request(self, path: str, *, method: str = "GET", body: dict | None = None, token: str | None = None) -> tuple[int, bytes, dict[str, str]]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            return error.code, error.read(), {k.lower(): v for k, v in error.headers.items()}

    def admin_token(self) -> str:
        status, body, _ = self.request("/api/auth/login", method="POST", body={"username": "review-admin", "password": "review-admin-password"})
        self.assertEqual(status, 200, body)
        return str(json.loads(body)["token"])

    def initialize(self) -> str:
        token = self.admin_token()
        status, body, _ = self.request("/api/ca/initialize", method="POST", body={"rootpass": "review-root-password"}, token=token)
        self.assertEqual(status, 200, body)
        return token

    def test_legacy_wrappers_keep_paths_and_reuse_keys(self) -> None:
        root = self.root / "gen_root_cert.sh"
        server = self.root / "gen_server_cert.sh"
        subprocess.run(["bash", str(root)], cwd=self.root, env=self.env, check=True, capture_output=True)
        subprocess.run(["bash", str(server), "legacy.example"], cwd=self.root, env=self.env, check=True, capture_output=True)
        legacy = self.root / "out" / "legacy.example"
        key = legacy / "rsa_legacy.example.key"
        self.assertTrue(key.is_file())
        first_key = key.resolve().read_bytes()
        subprocess.run(["bash", str(server), "legacy.example"], cwd=self.root, env=self.env, check=True, capture_output=True)
        self.assertEqual(first_key, key.resolve().read_bytes())
        for suffix in (".crt", ".key", ".bundle.crt", ".p12", ".pem"):
            self.assertTrue((legacy / f"rsa_legacy.example{suffix}").is_file(), suffix)

    def test_format_crt_returns_leaf_and_root_name_is_safe(self) -> None:
        token = self.initialize()
        status, body, _ = self.request("/api/certificates", method="POST", token=token, body={"type": "server", "name": "z.example", "sans": ["z.example"], "key_type": "rsa", "days": 30})
        self.assertEqual(status, 201, body)
        record = json.loads(body)
        status, leaf, _ = self.request(f"/api/certificates/{record['id']}/download?format=crt", token=token)
        self.assertEqual(status, 200)
        with tempfile.NamedTemporaryFile() as output:
            output.write(leaf); output.flush()
            subject = subprocess.check_output(["openssl", "x509", "-in", output.name, "-noout", "-subject"], text=True)
        self.assertIn("CN = z.example", subject)

        root_fingerprint_before = subprocess.check_output(["openssl", "x509", "-in", str(self.root / "out" / "rsa_root.crt"), "-noout", "-fingerprint"], text=True)
        status, root_body, _ = self.request("/api/certificates", method="POST", token=token, body={"type": "server", "name": "root", "sans": ["root.example"], "key_type": "rsa", "days": 30})
        self.assertEqual(status, 201, root_body)
        root_record = json.loads(root_body)
        leaf_path = self.root / root_record["output_dir"] / "rsa_root.crt"
        subject = subprocess.check_output(["openssl", "x509", "-in", str(leaf_path), "-noout", "-subject"], text=True)
        self.assertIn("CN = root", subject)
        root_fingerprint_after = subprocess.check_output(["openssl", "x509", "-in", str(self.root / "out" / "rsa_root.crt"), "-noout", "-fingerprint"], text=True)
        self.assertEqual(root_fingerprint_before, root_fingerprint_after)

    def test_options_advertises_patch(self) -> None:
        status, _body, headers = self.request("/api/agents", method="OPTIONS")
        self.assertEqual(status, 204)
        self.assertIn("PATCH", headers.get("access-control-allow-methods", ""))

    def test_duplicate_agent_rename_returns_conflict(self) -> None:
        token = self.admin_token()
        for name in ("alpha", "beta"):
            status, body, _ = self.request("/api/agents", method="POST", token=token, body={"name": name})
            self.assertEqual(status, 201, body)
        agents = self.server.store.list_agents()
        alpha = next(item for item in agents if item["name"] == "alpha")
        beta = next(item for item in agents if item["name"] == "beta")
        status, body, _ = self.request(f"/api/agents/{alpha['id']}", method="PATCH", token=token, body={"name": "beta"})
        self.assertEqual(status, 409, body)
        self.assertEqual(self.server.store.get_agent(alpha["id"])["name"], "alpha")
        self.assertEqual(self.server.store.get_agent(beta["id"])["name"], "beta")


if __name__ == "__main__":
    unittest.main()
