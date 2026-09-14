"""Вход в Mobbin в два шага: между командами не должно оставаться живого процесса."""

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from inspobot import mobbin_auth as ma
from inspobot.mobbin_auth import MobbinAuthError

META = {
    "authorization_endpoint": "https://example.test/authorize",
    "token_endpoint": "https://example.test/token",
    "registration_endpoint": "https://example.test/register",
}
MCP = "https://api.mobbin.com/mcp"


class TwoStepLoginTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.pending = Path(self.dir.name) / "auth_pending.json"
        self.tokens = Path(self.dir.name) / "mobbin_token.json"
        self.exchanged = {}

    def tearDown(self):
        self.dir.cleanup()

    def _token_request(self, client, endpoint, form, secret):
        self.exchanged.update(form)
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    def start(self):
        with mock.patch.object(ma, "discover", return_value=META), \
             mock.patch.object(ma, "register_client", return_value=("cid", "")):
            url = ma.begin_login(MCP, self.pending)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return url, query["state"][0], query["redirect_uri"][0]

    def finish(self, callback):
        with mock.patch.object(ma, "_token_request", side_effect=self._token_request):
            return ma.complete_login(MCP, self.pending, self.tokens, callback)

    def test_start_leaves_a_draft_and_returns(self):
        url, state, redirect = self.start()
        self.assertTrue(self.pending.exists())
        self.assertIn("code_challenge_method=S256", url)
        saved = json.loads(self.pending.read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], state)
        self.assertEqual(saved["redirect_uri"], redirect)
        self.assertNotIn("verifier", url, "проверочный код не должен утекать в ссылку")

    def test_finish_exchanges_and_cleans_up(self):
        _, state, redirect = self.start()
        tokens = self.finish(f"{redirect}?code=CODE-1&state={state}")
        self.assertEqual(tokens.refresh_token, "RT")
        self.assertEqual(self.exchanged["code"], "CODE-1")
        self.assertEqual(self.exchanged["redirect_uri"], redirect)
        self.assertTrue(self.exchanged["code_verifier"])
        self.assertTrue(self.tokens.exists())
        self.assertFalse(self.pending.exists(), "черновик убирается после успеха")

    def test_answer_from_an_earlier_login_is_refused(self):
        _, old_state, redirect = self.start()
        self.start()  # начали заново — прежний ответ больше не годится
        with self.assertRaises(MobbinAuthError) as caught:
            self.finish(f"{redirect}?code=CODE&state={old_state}")
        self.assertIn("state", str(caught.exception))

    def test_garbage_instead_of_an_address(self):
        self.start()
        for text in ("просто текст", "", "https://mobbin.com/"):
            with self.assertRaises(MobbinAuthError, msg=text):
                self.finish(text)

    def test_refusal_from_mobbin_is_readable(self):
        _, state, redirect = self.start()
        with self.assertRaises(MobbinAuthError) as caught:
            self.finish(f"{redirect}?error=access_denied&state={state}")
        self.assertIn("access_denied", str(caught.exception))

    def test_finish_without_start_says_what_to_do(self):
        with self.assertRaises(MobbinAuthError) as caught:
            self.finish("http://127.0.0.1:1/callback?code=a&state=b")
        self.assertIn("--start", str(caught.exception))

    def test_draft_is_not_world_readable(self):
        self.start()
        self.assertEqual(self.pending.stat().st_mode & 0o077, 0, "черновик виден чужим")
