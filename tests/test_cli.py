import contextlib
import io
import json
import os
import tempfile
import unittest
import base64
from unittest import mock
from pathlib import Path

from ocx_import.cli import main


def session(email="a@example.com", account="acct-a", token="access-a", refresh=None):
    value = {"accessToken": token, "user": {"email": email}, "account": {"id": account}}
    if refresh:
        value["refreshToken"] = refresh
    return value


def valid_jwt(account="acct-a", email="a@example.com", exp=4102444800):
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()
    payload = {"exp": exp, "chatgpt_account_id": account, "email": email}
    return f"{enc({'alg':'none'})}.{enc(payload)}.sig"


class CliTests(unittest.TestCase):
    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_version_and_invalid_args(self):
        code, out, err = self.invoke(["--version"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        code, out, err = self.invoke([])
        self.assertEqual(code, 2)

    def test_dry_run_writes_nothing_and_summary_hides_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            root, src = Path(td) / "home", Path(td) / "input.json"
            src.write_text(json.dumps(session(token="SECRET_ACCESS", refresh="SECRET_REFRESH")), encoding="utf-8")
            code, out, err = self.invoke(["--home", str(root), "--dry-run", str(src)])
            self.assertEqual(code, 0, err)
            self.assertFalse(root.exists())
            self.assertNotIn("SECRET_ACCESS", out)
            self.assertNotIn("SECRET_REFRESH", out)
            data = json.loads(out)
            self.assertEqual(data["imported"][0]["email"], "a@example.com")

    def test_multi_file_validation_is_atomic_and_no_activate(self):
        with tempfile.TemporaryDirectory() as td:
            root, good, bad = Path(td) / "home", Path(td) / "good.json", Path(td) / "bad.json"
            good.write_text(json.dumps(session(refresh="r-good")), encoding="utf-8")
            bad.write_text("{not json", encoding="utf-8")
            code, out, err = self.invoke(["--home", str(root), str(good), str(bad)])
            self.assertEqual(code, 1)
            self.assertFalse(root.exists())

            bad.write_text(json.dumps(session(email="b@example.com", account="acct-b", token="access-b", refresh="r-bad")), encoding="utf-8")
            code, out, err = self.invoke(["--home", str(root), "--no-activate", str(good), str(bad)])
            self.assertEqual(code, 0, err)
            self.assertTrue((root / "config.json").exists())
            config = json.loads((root / "config.json").read_text())
            self.assertEqual(len(config.get("codexAccounts", [])), 2)
            self.assertIn("defaultProvider", config)

    def test_repeat_import_keeps_existing_refresh_token(self):
        with tempfile.TemporaryDirectory() as td:
            root, src1, src2 = Path(td) / "home", Path(td) / "one.json", Path(td) / "two.json"
            src1.write_text(json.dumps(session(token="access-a", refresh="refresh-a")), encoding="utf-8")
            self.assertEqual(self.invoke(["--home", str(root), str(src1)])[0], 0)
            src2.write_text(json.dumps(session(token=valid_jwt(), refresh=None)), encoding="utf-8")
            self.assertEqual(self.invoke(["--home", str(root), str(src2)])[0], 0)
            store = json.loads((root / "codex-accounts.json").read_text())
            self.assertEqual(len(store), 1)
            cred = next(iter(store.values()))["credential"]
            self.assertEqual(cred["accessToken"], valid_jwt())
            self.assertEqual(cred["refreshToken"], "refresh-a")

    def test_bom_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root, src = Path(td) / "home", Path(td) / "bom.json"
            src.write_bytes(b"\xef\xbb\xbf" + json.dumps(session(refresh="r")).encode())
            code, _, err = self.invoke(["--home", str(root), str(src)])
            self.assertEqual(code, 0, err)

    def test_duplicate_accounts_in_input_use_last_access_token(self):
        with tempfile.TemporaryDirectory() as td:
            root, src1, src2 = Path(td) / "home", Path(td) / "one.json", Path(td) / "two.json"
            src1.write_text(json.dumps(session(token="first", refresh="keep")), encoding="utf-8")
            src2.write_text(json.dumps(session(token=valid_jwt(), refresh=None)), encoding="utf-8")
            code, _, err = self.invoke(["--home", str(root), str(src1), str(src2)])
            self.assertEqual(code, 0, err)
            store = json.loads((root / "codex-accounts.json").read_text())
            cred = next(iter(store.values()))["credential"]
            self.assertEqual(cred["accessToken"], valid_jwt())
            self.assertEqual(cred["refreshToken"], "keep")

    def test_env_home_and_explicit_home_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            env_home, explicit, src = Path(td) / "env", Path(td) / "explicit", Path(td) / "s.json"
            src.write_text(json.dumps(session(refresh="r")), encoding="utf-8")
            with mock.patch.dict(os.environ, {"OPENCODEX_HOME": str(env_home)}, clear=False):
                code, out, err = self.invoke(["--dry-run", str(src)])
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["home"], str(env_home))
                code, out, err = self.invoke(["--dry-run", "--home", str(explicit), str(src)])
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["home"], str(explicit))

    def test_malformed_json_error_does_not_leak_token(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "bad.json"
            secret = "SECRET_FRAGMENT"
            src.write_text('{"accessToken":"' + secret, encoding="utf-8")
            code, out, err = self.invoke([str(src)])
            self.assertEqual(code, 1)
            self.assertNotIn(secret, err)

    def test_write_failure_is_clean_error(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "s.json"
            src.write_text(json.dumps(session(refresh="r")), encoding="utf-8")
            with mock.patch("ocx_import.cli.apply_accounts", side_effect=OSError("disk full")):
                code, out, err = self.invoke(["--home", str(Path(td) / "home"), str(src)])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)

    def test_mixed_session_and_sub2api_files(self):
        with tempfile.TemporaryDirectory() as td:
            root, one, two = Path(td) / "home", Path(td) / "one.json", Path(td) / "two.json"
            one.write_text(json.dumps(session(token=valid_jwt(), refresh=None)), encoding="utf-8")
            two.write_text(json.dumps({"accounts": [{"platform": "openai", "credentials": {"accessToken": "sub", "refreshToken": "r-sub", "chatgptAccountId": "sub-acct", "email": "sub@x"}}]}), encoding="utf-8")
            code, out, err = self.invoke(["--home", str(root), str(one), str(two)])
            self.assertEqual(code, 0, err)
            self.assertEqual(len(json.loads(out)["imported"]), 2)


if __name__ == "__main__":
    unittest.main()
