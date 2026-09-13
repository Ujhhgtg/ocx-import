import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ocx_import.models import Account, ImportError
from ocx_import.target import apply_accounts


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def account(self, **kw):
        vals = dict(account_id="workspace", email="User@Example.com", access_token="access", refresh_token="refresh", user_id="user")
        vals.update(kw)
        return Account(**vals)

    def test_import_schema_provider_and_generation(self):
        result = apply_accounts(self.root, [self.account()])
        self.assertEqual(result["imported"][0]["refreshable"], True)
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual(config["providers"]["openai"]["codexAccountMode"], "pool")
        self.assertEqual(config["activeCodexAccountId"], self.account().identity_id)
        record = json.loads((self.root / "codex-accounts.json").read_text())[self.account().identity_id]
        self.assertEqual(record["generation"], 0)
        self.assertEqual(len(record["refreshGrantFingerprint"]), 64)

    def test_upsert_retains_refresh_and_increments_generation(self):
        apply_accounts(self.root, [self.account()])
        apply_accounts(self.root, [self.account(access_token="new", refresh_token="")])
        record = json.loads((self.root / "codex-accounts.json").read_text())[self.account().identity_id]
        self.assertEqual(record["credential"]["accessToken"], "new")
        self.assertEqual(record["credential"]["refreshToken"], "refresh")
        self.assertEqual(record["generation"], 1)

    def test_team_users_are_distinct(self):
        apply_accounts(self.root, [self.account(user_id="u1"), self.account(user_id="u2")])
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual(len(config["codexAccounts"]), 2)

    def test_email_legacy_upgrade_creates_alias(self):
        legacy = self.account(user_id="")
        self.root.joinpath("config.json").write_text(json.dumps({"codexAccounts": [{"id": legacy.identity_id, "email": legacy.email, "chatgptAccountId": legacy.account_id, "isMain": False}]}))
        self.root.joinpath("codex-accounts.json").write_text(json.dumps({legacy.identity_id: {"credential": {"refreshToken": "old"}}}))
        result = apply_accounts(self.root, [self.account(user_id="new-user", refresh_token="")])
        self.assertEqual(result["imported"][0]["id"], legacy.identity_id)
        aliases = json.loads((self.root / "ocx-import-identities.json").read_text())
        self.assertIn(legacy.identity_id, aliases["codexAccounts"].values())

    def test_dry_run_does_not_create_files(self):
        result = apply_accounts(self.root / "missing", [self.account()], dry_run=True)
        self.assertEqual(result["backup_path"], None)
        self.assertFalse((self.root / "missing").exists())

    def test_malformed_preserves_bytes(self):
        path = self.root / "config.json"; path.write_bytes(b"[]\n")
        with self.assertRaises(ImportError): apply_accounts(self.root, [self.account()])
        self.assertEqual(path.read_bytes(), b"[]\n")

    def test_no_activate(self):
        original = self.account()
        second = self.account(account_id="workspace-2")
        apply_accounts(self.root, [original, second], activate=False)
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual(config["activeCodexAccountId"], original.identity_id)
        apply_accounts(self.root, [second], activate=False)
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual(config["activeCodexAccountId"], original.identity_id)

    def test_backup_permissions_and_unique(self):
        first = apply_accounts(self.root, [self.account()]); second = apply_accounts(self.root, [self.account(access_token="2")])
        self.assertNotEqual(first["backup_path"], second["backup_path"])
        self.assertEqual(stat.S_IMODE(os.stat(first["backup_path"]).st_mode), 0o700)
        for p in Path(first["backup_path"]).iterdir(): self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_rollback_on_write_failure(self):
        with patch("ocx_import.target._write", side_effect=[OSError("boom")]):
            with self.assertRaises(OSError): apply_accounts(self.root, [self.account()])
        self.assertFalse((self.root / "config.json").exists())
        self.assertFalse((self.root / "codex-accounts.json").exists())

    def test_rollback_restores_existing_bytes_and_generation(self):
        apply_accounts(self.root, [self.account()])
        before = {n: (self.root / n).read_bytes() for n in ("config.json", "codex-accounts.json")}
        db_before = (self.root / "config-mutation.sqlite").read_bytes()
        with patch("ocx_import.target._write", side_effect=[lambda p,v: None, OSError("boom")]):
            with self.assertRaises(OSError): apply_accounts(self.root, [self.account(access_token="changed")])
        self.assertEqual(before["config.json"], (self.root / "config.json").read_bytes())
        self.assertEqual(before["codex-accounts.json"], (self.root / "codex-accounts.json").read_bytes())

    def test_preserves_unrelated_fields(self):
        self.root.joinpath("config.json").write_text(json.dumps({"other": 42, "codexAccounts": []}))
        apply_accounts(self.root, [self.account()])
        self.assertEqual(json.loads((self.root / "config.json").read_text())["other"], 42)

    def test_malformed_alias_and_provider_rejected(self):
        p = self.root / "ocx-import-identities.json"; p.write_text(json.dumps({"schemaVersion": 2, "codexAccounts": {}})); old = p.read_bytes()
        with self.assertRaises(ImportError): apply_accounts(self.root, [self.account()])
        self.assertEqual(p.read_bytes(), old)

    def test_agent_identity_kind_rejected(self):
        aid = self.account().identity_id
        self.root.joinpath("config.json").write_text(json.dumps({"codexAccounts": [{"id": aid}]}))
        self.root.joinpath("codex-accounts.json").write_text(json.dumps({aid: {"credential": {"authMode": "agentIdentity"}}}))
        with self.assertRaises(ImportError): apply_accounts(self.root, [self.account()])


if __name__ == "__main__":
    unittest.main()
