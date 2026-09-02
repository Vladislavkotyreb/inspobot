"""Сквозная проверка оркестровки без сети: Claude и Telegram подменены."""

import asyncio
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from inspobot import daily
from inspobot.config import Config
from inspobot.models import Digest, Pick
from inspobot.state import Store
from inspobot.topics import topics_for

DAY = date(2026, 9, 2)
MOBILE, DESKTOP = topics_for(DAY)


def make_picks(n_ios=2, n_web=3):
    picks = []
    for i in range(n_ios + n_web):
        sid = f"{i:08d}-0000-4000-8000-000000000000"
        picks.append(
            Pick(
                platform="ios" if i < n_ios else "web",
                screen_id=sid,
                mobbin_url=f"https://mobbin.com/screens/{sid}",
                image_url=f"https://mobbin.com/api/mcp/short/{i}",
                app_name=f"App {i}",
                pattern="Приём",
                note="Заметка.",
            )
        )
    return tuple(picks)


class FakeTelegram:
    """Считает отправленное и умеет притворяться, что картинка не дошла."""

    def __init__(self, token, chat_id, photo_ok=True):
        self.messages: list[str] = []
        self.photos: list[str] = []
        self.photo_ok = photo_ok

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def close(self):
        return None

    async def send_message(self, text, chat_id=None):
        self.messages.append(text)
        return {}

    async def send_photo(self, image_url, caption, chat_id=None):
        if self.photo_ok:
            self.photos.append(image_url)
            return True
        return False

    async def pause(self):
        return None


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        base = Config.from_env()
        self.config = Config(
            **{
                **base.__dict__,
                "telegram_token": "t",
                "telegram_chat_id": "42",
                "anthropic_api_key": "k",
                "db_path": Path(self.dir.name) / "state.sqlite3",
                "mobbin_access_token": "mobbin-token",
            }
        )
        self.digest = Digest(DAY, MOBILE, DESKTOP, "Итог дня.", make_picks())

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, telegram, **kwargs):
        with mock.patch.object(daily, "collect", return_value=self.digest) as collect, \
             mock.patch.object(daily, "Telegram", return_value=telegram):
            sent = asyncio.run(daily.run_once(self.config, day=DAY, **kwargs))
        return sent, collect

    def test_sends_header_and_one_photo_per_pick(self):
        telegram = FakeTelegram("t", "42")
        sent, _ = self._run(telegram)
        self.assertEqual(sent, 5)
        self.assertEqual(len(telegram.photos), 5)
        self.assertEqual(len(telegram.messages), 1)  # только шапка
        self.assertIn("Итог дня.", telegram.messages[0])

    def test_falls_back_to_text_when_photo_fails(self):
        telegram = FakeTelegram("t", "42", photo_ok=False)
        sent, _ = self._run(telegram)
        self.assertEqual(sent, 5)
        self.assertEqual(len(telegram.photos), 0)
        self.assertEqual(len(telegram.messages), 6)  # шапка + пять текстовых

    def test_second_run_same_day_is_skipped(self):
        self._run(FakeTelegram("t", "42"))
        telegram = FakeTelegram("t", "42")
        sent, collect = self._run(telegram)
        self.assertEqual(sent, 0)
        self.assertEqual(collect.call_count, 0)
        self.assertEqual(telegram.messages, [])

    def test_force_reruns_and_passes_seen_ids_to_the_model(self):
        self._run(FakeTelegram("t", "42"))
        telegram = FakeTelegram("t", "42")
        _, collect = self._run(telegram, force=True)
        self.assertEqual(collect.call_count, 1)
        ios_seen, web_seen = collect.call_args.args[-2], collect.call_args.args[-1]
        self.assertEqual(len(ios_seen), 2)
        self.assertEqual(len(web_seen), 3)

    def test_marks_screens_as_seen(self):
        self._run(FakeTelegram("t", "42"))
        store = Store(self.config.db_path)
        self.assertEqual(len(store.recent_seen_ids("ios")), 2)
        self.assertEqual(len(store.recent_seen_ids("web")), 3)
        self.assertTrue(store.sent_today(DAY))

    def test_failure_is_reported_and_day_stays_open(self):
        telegram = FakeTelegram("t", "42")
        with mock.patch.object(daily, "collect", side_effect=RuntimeError("Mobbin молчит")), \
             mock.patch.object(daily, "Telegram", return_value=telegram):
            with self.assertRaises(RuntimeError):
                asyncio.run(daily.run_once(self.config, day=DAY))
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Mobbin молчит", telegram.messages[0])
        self.assertFalse(Store(self.config.db_path).sent_today(DAY))

    def test_dry_run_sends_nothing(self):
        telegram = FakeTelegram("t", "42")
        sent, _ = self._run(telegram, dry_run=True)
        self.assertEqual(sent, 5)
        self.assertEqual(telegram.messages, [])
        self.assertEqual(telegram.photos, [])
        self.assertFalse(Store(self.config.db_path).sent_today(DAY))


if __name__ == "__main__":
    unittest.main()
