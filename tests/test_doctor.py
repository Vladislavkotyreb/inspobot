import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from inspobot import doctor
from inspobot.config import Config
from inspobot.mobbin_auth import Tokens, save_tokens


def update(chat_id, chat_type="private", **chat):
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": chat_type, **chat}}}


class ChatsFromUpdatesTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(doctor.chats_from_updates([]), [])

    def test_deduplicates_and_names(self):
        chats = doctor.chats_from_updates(
            [
                update(42, first_name="Влад", last_name="К"),
                update(42, first_name="Влад", last_name="К"),
                update(-100, "channel", title="Референсы"),
            ]
        )
        self.assertEqual([c["id"] for c in chats], ["42", "-100"])
        self.assertEqual(chats[0]["title"], "Влад К")
        self.assertEqual(chats[1]["type"], "channel")

    def test_skips_updates_without_a_chat(self):
        self.assertEqual(doctor.chats_from_updates([{"update_id": 1}]), [])

    def test_reads_channel_posts(self):
        chats = doctor.chats_from_updates(
            [{"update_id": 1, "channel_post": {"chat": {"id": -5, "type": "channel"}}}]
        )
        self.assertEqual(chats[0]["id"], "-5")


class MobbinCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        self.path = Path(self.dir.name) / "mobbin_token.json"
        self.config = Config(**{**Config.from_env().__dict__, "mobbin_token_file": self.path})

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, refresh="r", ttl=3600):
        save_tokens(
            self.path,
            Tokens("a", refresh, time.time() + ttl, "cid", "", "https://as/token"),
        )

    def test_missing_file_is_a_failure(self):
        self.assertEqual(doctor.check_mobbin(self.config).status, doctor.FAIL)

    def test_env_override_wins(self):
        config = Config(**{**self.config.__dict__, "mobbin_access_token": "tok"})
        self.assertEqual(doctor.check_mobbin(config).status, doctor.OK)

    def test_token_with_refresh_is_fine(self):
        self._write()
        self.assertEqual(doctor.check_mobbin(self.config).status, doctor.OK)

    def test_token_without_refresh_warns(self):
        self._write(refresh="")
        self.assertEqual(doctor.check_mobbin(self.config).status, doctor.WARN)

    def test_expired_token_without_refresh_fails(self):
        self._write(refresh="", ttl=-10)
        self.assertEqual(doctor.check_mobbin(self.config).status, doctor.FAIL)


class TelegramCheckTest(unittest.TestCase):
    def setUp(self):
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        self.base = Config.from_env().__dict__

    def _telegram(self, me=None, updates=None, boom=False):
        fake = mock.MagicMock()
        fake.__aenter__ = mock.AsyncMock(return_value=fake)
        fake.__aexit__ = mock.AsyncMock(return_value=None)
        fake.get_me = mock.AsyncMock(
            side_effect=RuntimeError("401 Unauthorized") if boom else None,
            return_value=me or {"id": 1, "username": "inspo_bot"},
        )
        fake.get_updates = mock.AsyncMock(return_value=updates or [])
        return fake

    def _run(self, config, telegram):
        with mock.patch.object(doctor, "Telegram", return_value=telegram):
            return asyncio.run(doctor.check_telegram(config))

    def test_missing_token(self):
        config = Config(**{**self.base, "telegram_token": ""})
        self.assertEqual(self._run(config, self._telegram())[0].status, doctor.FAIL)

    def test_bad_token_reported_once(self):
        config = Config(**{**self.base, "telegram_token": "x"})
        results = self._run(config, self._telegram(boom=True))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, doctor.FAIL)

    def test_known_chat_id_is_not_rediscovered(self):
        config = Config(**{**self.base, "telegram_token": "x", "telegram_chat_id": "42"})
        telegram = self._telegram()
        results = self._run(config, telegram)
        self.assertEqual([r.status for r in results], [doctor.OK, doctor.OK])
        telegram.get_updates.assert_not_called()

    def test_suggests_chat_id_from_updates(self):
        config = Config(**{**self.base, "telegram_token": "x", "telegram_chat_id": ""})
        results = self._run(config, self._telegram(updates=[update(42, first_name="Влад")]))
        self.assertEqual(results[1].status, doctor.WARN)
        self.assertIn("42", results[1].detail)

    def test_asks_to_write_the_bot_when_no_updates(self):
        config = Config(**{**self.base, "telegram_token": "x", "telegram_chat_id": ""})
        results = self._run(config, self._telegram(updates=[]))
        self.assertEqual(results[1].status, doctor.FAIL)


if __name__ == "__main__":
    unittest.main()


class ExplainAnthropicErrorTest(unittest.TestCase):
    def test_workspace_error_gets_instructions(self):
        message = doctor.explain_anthropic_error(
            RuntimeError(
                "Error code: 400 - {'message': 'This API key is not scoped to a "
                "workspace, so this request must include the anthropic-workspace-id header'}"
            )
        )
        self.assertIn("workspace", message)
        self.assertIn("ANTHROPIC_WORKSPACE_ID", message)
        self.assertNotIn("Error code", message)

    def test_empty_balance(self):
        message = doctor.explain_anthropic_error(RuntimeError("Your credit balance is too low"))
        self.assertIn("Billing", message)

    def test_bad_key(self):
        message = doctor.explain_anthropic_error(RuntimeError("invalid x-api-key"))
        self.assertIn("sk-ant-", message)

    def test_unknown_error_is_passed_through(self):
        message = doctor.explain_anthropic_error(ValueError("что-то новое"))
        self.assertIn("ValueError", message)
        self.assertIn("что-то новое", message)


class MakeClientTest(unittest.TestCase):
    def setUp(self):
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        self.base = Config.from_env().__dict__

    def test_header_added_only_when_workspace_is_set(self):
        from inspobot.client import WORKSPACE_HEADER, make_client

        without = make_client(Config(**{**self.base, "anthropic_api_key": "k"}))
        self.assertNotIn(WORKSPACE_HEADER, without.default_headers)

        with_ws = make_client(
            Config(**{**self.base, "anthropic_api_key": "k", "anthropic_workspace_id": "wrkspc_1"})
        )
        self.assertEqual(with_ws.default_headers[WORKSPACE_HEADER], "wrkspc_1")


class LoggingSetupTest(unittest.TestCase):
    """--verbose не должен включать отладку SDK: в теле запроса лежит токен Mobbin."""

    def tearDown(self):
        import logging

        from inspobot.logs import NOISY

        for name in ("inspobot", *NOISY):
            logging.getLogger(name).setLevel(logging.NOTSET)

    def test_verbose_raises_only_our_own_logger(self):
        import logging

        from inspobot import logs

        logs.setup(verbose=True)
        self.assertEqual(logging.getLogger("inspobot").level, logging.DEBUG)
        for name in logs.NOISY:
            self.assertEqual(
                logging.getLogger(name).level, logging.WARNING, f"{name} печатал бы тело запроса"
            )

    def test_quiet_mode_keeps_our_logger_at_info(self):
        import logging

        from inspobot import logs

        logs.setup(verbose=False)
        self.assertEqual(logging.getLogger("inspobot").level, logging.INFO)
