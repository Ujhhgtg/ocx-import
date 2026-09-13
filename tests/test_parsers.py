import base64
import json
import time
import unittest

from ocx_import.models import Account, ImportError
from ocx_import.parsers import parse_document


def jwt(payload):
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.sig"


class ParserTests(unittest.TestCase):
    def test_session_jwt_and_namespaced_identity(self):
        token = jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_user_id": "user-1", "chatgpt_plan_type": "plus"}, "https://api.openai.com/profile": {"email": "a@example.com"}, "exp": 4102444800})
        result = parse_document({"accessToken": token, "user": {"id": "user-1", "email": "a@example.com"}, "account": {"id": "acct-1", "planType": "plus"}}, source="session")
        self.assertEqual(len(result), 1)
        account = result[0]
        self.assertIsInstance(account, Account)
        self.assertEqual((account.account_id, account.user_id, account.email, account.plan), ("acct-1", "user-1", "a@example.com", "plus"))
        self.assertEqual(account.expires_at, 4102444800000)

    def test_aliases_and_refreshable_expiry(self):
        result = parse_document({"access_token": "opaque-access", "refresh_token": "refresh", "user": {"email": "x@y.test"}, "account": {"id": "acct"}, "expires": "2030-01-01T00:00:00Z"})
        self.assertEqual(result[0].access_token, "opaque-access")
        self.assertEqual(result[0].refresh_token, "refresh")
        self.assertGreater(result[0].expires_at, 0)

    def test_expired_jwt_rejected_for_no_refresh_even_if_session_expiry_future(self):
        token = jwt({"exp": 100, "https://api.openai.com/auth": {"chatgpt_account_id": "a"}, "https://api.openai.com/profile": {"email": "e@x"}})
        with self.assertRaises(ImportError):
            parse_document({"accessToken": token, "account": {"id": "a"}, "user": {"email": "e@x"}, "expires": 4102444800}, now=1000)

    def test_identity_mismatch_rejected(self):
        token = jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "jwt-a", "chatgpt_user_id": "jwt-u"}, "https://api.openai.com/profile": {"email": "e@x"}, "exp": 4102444800})
        with self.assertRaises(ImportError):
            parse_document({"accessToken": token, "account": {"id": "different"}, "user": {"id": "jwt-u", "email": "e@x"}}, now=0)

    def test_top_level_claims_and_id_token_fallback(self):
        access = jwt({"chatgpt_account_id": "acct-top", "exp": 4102444800})
        ident = jwt({"chatgpt_user_id": "user-id", "https://api.openai.com/profile": {"email": "id@x"}})
        result = parse_document({"accessToken": access, "idToken": ident, "account": {"id": "acct-top"}, "user": {"id": "user-id"}}, now=0)
        self.assertEqual((result[0].account_id, result[0].user_id, result[0].email), ("acct-top", "user-id", "id@x"))

    def test_expiry_units_and_invalid_exp(self):
        for exp, expected in ((4102444800, 4102444800000), (4102444800000, 4102444800000), ("2030-01-01T00:00:00+02:00", 1893448800000)):
            account = parse_document({"accessToken": "opaque", "refreshToken": "refresh", "expires": exp, "account": {"id": "a"}, "user": {"email": "e@x"}}, now=0)[0]
            self.assertEqual(account.expires_at, expected)
        for exp in (True, 10**30, "nan"):
            with self.assertRaises(ImportError):
                parse_document({"accessToken": jwt({"exp": exp, "chatgpt_account_id": "a", "email": "e@x"}), "account": {"id": "a"}, "user": {"email": "e@x"}}, now=0)

    def test_each_account_keeps_its_own_expiry(self):
        rows = [
            {"accessToken": "one", "refreshToken": "r1", "expires": 4102444800, "account": {"id": "a1"}, "user": {"email": "one@x"}},
            {"accessToken": "two", "refreshToken": "r2", "expires": 4102448400000, "account": {"id": "a2"}, "user": {"email": "two@x"}},
        ]
        accounts = parse_document(rows)
        self.assertEqual([account.expires_at for account in accounts], [4102444800000, 4102448400000])

    def test_opaque_access_requires_refresh_despite_session_expiry(self):
        for refresh in (None, "   ", {"token": "invalid"}):
            document = {
                "accessToken": "not-a.valid.jwt",
                "expires": 4102444800,
                "account": {"id": "a"},
                "user": {"email": "e@x"},
            }
            if refresh is not None:
                document["refreshToken"] = refresh
            with self.subTest(refresh=refresh), self.assertRaises(ImportError):
                parse_document(document)

    def test_sub2api_envelope_and_mixed_array(self):
        row = {"platform": "openai", "credentials": {"accessToken": "a", "refreshToken": "r", "chatgptAccountId": "acct", "email": "e@x"}}
        result = parse_document({"accounts": [row]}, source="sub2api")
        self.assertEqual(result[0].account_id, "acct")
        mixed = parse_document([{"accessToken": "b", "refreshToken": "rb", "account": {"id": "b"}, "user": {"email": "b@x"}}, row])
        self.assertEqual(len(mixed), 2)

    def test_unrelated_provider_and_malformed_values_rejected(self):
        with self.assertRaises(ImportError):
            parse_document({"platform": "anthropic", "credentials": {"accessToken": "x"}})
        with self.assertRaises(ImportError):
            parse_document([])
        with self.assertRaises(ImportError):
            parse_document({"user": {"email": "e@x"}})

if __name__ == "__main__":
    unittest.main()
