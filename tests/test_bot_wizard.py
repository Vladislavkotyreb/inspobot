"""Обработка нажатий на кнопки мастера — без Telegram и без Claude."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inspobot import bot, wizard
from inspobot.config import Config
from inspobot.models import Pick
from inspobot.state import Store
from inspobot.wizard import Selection

PICKS = (
    Pick("ios", "ed0c23e4-d2c7-428f-bf9c-d6efd38d7f47",
         "https://mobbin.com/screens/ed0c23e4-d2c7-428f-bf9c-d6efd38d7f47",
         "https://mobbin.com/api/mcp/short/x", "Claude", "Приём", "Заметка."),
)


class FakeTelegram:
    def __init__(self):
        self.answers: list[str] = []
        self.edits: list[tuple[str, dict | None]] = []
        self.photos: list[str] = []
        self.keyboards: list[dict] = []

    async def answer_callback(self, callback_id, text=""):
        self.answers.append(text)

    async def edit_message(self, chat_id, message_id, text, keyboard=None):
        self.edits.append((text, keyboard))

    async def send_message_with_keyboard(self, text, keyboard, chat_id=None):
        self.keyboards.append(keyboard)

    async def send_message(self, text, chat_id=None):
        self.edits.append((text, None))

    async def send_photo(self, url, caption, chat_id=None):
        self.photos.append(url)
        return True

    async def pause(self):
        return None


def query(data, chat_id="42", message_id=7):
    return {
        "id": "cb1",
        "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat_id}},
    }


class CallbackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        base = Config.from_env().__dict__
        self.config = Config(
            **{
                **base,
                "telegram_token": "t",
                "telegram_chat_id": "42",
                "anthropic_api_key": "k",
                "mobbin_access_token": "m",
                "db_path": Path(self.dir.name) / "state.sqlite3",
            }
        )
        self.telegram = FakeTelegram()

    def tearDown(self):
        self.dir.cleanup()

    def run_callback(self, data, **kwargs):
        asyncio.run(bot.handle_callback(self.config, self.telegram, query(data, **kwargs)))

    def test_intermediate_step_edits_message_with_new_keyboard(self):
        self.run_callback(wizard.encode(Selection("s")))
        text, keyboard = self.telegram.edits[-1]
        self.assertIn("Платформа?", text)
        self.assertIsNotNone(keyboard)
        self.assertEqual(self.telegram.photos, [])

    def test_stale_button_is_answered_not_crashed(self):
        self.run_callback("что-то не наше")
        self.assertEqual(self.telegram.answers, [bot.STALE_BUTTON])
        self.assertEqual(self.telegram.edits, [])

    def test_foreign_chat_is_refused(self):
        self.run_callback(wizard.encode(Selection("s")), chat_id="999")
        self.assertEqual(len(self.telegram.answers), 1)
        self.assertNotEqual(self.telegram.answers[0], "")
        self.assertEqual(self.telegram.edits, [])

    def test_complete_selection_runs_the_search_and_sends_picks(self):
        with mock.patch.object(
            bot, "collect_selection", return_value=("Итог.", PICKS)
        ) as collect:
            self.run_callback(wizard.encode(Selection("s", "i", "b", "dashboard")))

        self.assertEqual(collect.call_count, 1)
        passed_selection = collect.call_args.args[1]
        self.assertEqual(passed_selection, Selection("s", "i", "b", "dashboard"))
        self.assertEqual(self.telegram.photos, [PICKS[0].image_url])

        texts = [t for t, _ in self.telegram.edits]
        self.assertIn("Ищу…", texts[0])
        self.assertIn("Нашлось: 1", texts[-1])

    def test_screens_from_the_wizard_are_remembered(self):
        with mock.patch.object(bot, "collect_selection", return_value=("", PICKS)):
            self.run_callback(wizard.encode(Selection("s", "i", "b", "dashboard")))
        self.assertEqual(Store(self.config.db_path).recent_seen_ids("ios"), [PICKS[0].screen_id])

    def test_sections_do_not_pollute_the_seen_list(self):
        with mock.patch.object(bot, "collect_selection", return_value=("", PICKS)):
            self.run_callback(wizard.encode(Selection("w", "", "b", "pricing")))
        self.assertEqual(Store(self.config.db_path).recent_seen_ids("ios"), [])

    def test_failure_is_reported_in_place(self):
        with mock.patch.object(bot, "collect_selection", side_effect=RuntimeError("Mobbin молчит")):
            self.run_callback(wizard.encode(Selection("f", "i", "c", "purchase")))
        self.assertIn("Mobbin молчит", self.telegram.edits[-1][0])


class StartWizardTest(unittest.TestCase):
    def test_first_message_carries_the_kind_keyboard(self):
        telegram = FakeTelegram()
        asyncio.run(bot.start_wizard(telegram, "42"))
        rows = telegram.keyboards[0]["inline_keyboard"]
        self.assertEqual(len(rows[0]), 3)


if __name__ == "__main__":
    unittest.main()
