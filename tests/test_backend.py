import tempfile
import unittest
from pathlib import Path

from server import CertificateError, CertificateStore


class BackendIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = CertificateStore(root, root / "out" / "certificates.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_platform_migration_and_password_session(self):
        platform = self.store.platform_agent_id
        self.assertEqual(self.store.get_agent(platform)["name"], "platform")
        user = self.store.create_user("admin", "correct horse battery", "admin")
        self.assertTrue(self.store.verify_password("correct horse battery", self.store.find_user("admin")["password_hash"]))
        token, _ = self.store.create_session(user["id"])
        self.assertEqual(self.store.actor_for_token(token)["username"], "admin")

    def test_agent_user_requires_active_agent(self):
        agent = self.store.create_agent("tenant-a")
        user = self.store.create_user("tenant", "a-secure-password", "agent", agent["id"])
        self.assertEqual(user["agent_id"], agent["id"])
        self.store.update_agent(agent["id"], {"status": "disabled"})
        token, _ = self.store.create_session(user["id"])
        self.assertIsNone(self.store.actor_for_token(token))

    def test_platform_cannot_be_disabled_or_renamed(self):
        with self.assertRaises(CertificateError):
            self.store.update_agent(self.store.platform_agent_id, {"status": "disabled"})
        with self.assertRaises(CertificateError):
            self.store.update_agent(self.store.platform_agent_id, {"name": "renamed"})


if __name__ == "__main__":
    unittest.main()
