"""Служба кнопок: нажатие превращается в топ, чужие — нет."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inspobot import listener
from inspobot.config import Config
from inspobot.render import TOP_MONTH, TOP_WEEK


class FakeTelegram:
    def __init__(self):
        self.answers: list[str] = []
        self.messages: list[tuple[str, str | None]] = []

    async def answer_callback(self, callback_id, text=""):
        self.answers.append(text)

    async def send_message(self, text, chat_id=None, keyboard=None):
        self.messages.append((text, chat_id))
        return {"message_id": 1}


def press(data, chat_id="42"):
    return {
        "update_id": 1,
        "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": chat_id}}},
    }


def typed(text, chat_id="42"):
    return {"update_id": 2, "message": {"chat": {"id": chat_id}, "text": text}}


class HandleUpdateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        self.config = Config(
            **{
                **Config.from_env().__dict__,
                "telegram_token": "t",
                "telegram_chat_id": "42",
                "chats_path": Path(self.dir.name) / "chats.txt",
                "db_path": Path(self.dir.name) / "state.sqlite3",
            }
        )
        self.telegram = FakeTelegram()

    def tearDown(self):
        self.dir.cleanup()

    def handle(self, update, lock=None):
        lock = lock or asyncio.Lock()
        with mock.patch.object(listener, "run_top", new=mock.AsyncMock()) as run:
            asyncio.run(listener.handle_update(self.config, self.telegram, update, lock))
        return run

    def test_week_button_starts_the_week_top_for_that_chat(self):
        run = self.handle(press(TOP_WEEK))
        run.assert_awaited_once()
        self.assertEqual(run.await_args.args[1], "week")
        self.assertEqual(run.await_args.kwargs["chat_id"], "42")
        self.assertEqual(self.telegram.answers, [listener.STARTED])

    def test_month_button(self):
        run = self.handle(press(TOP_MONTH))
        self.assertEqual(run.await_args.args[1], "month")

    def test_unknown_button_is_answered_not_run(self):
        run = self.handle(press("что-то чужое"))
        run.assert_not_awaited()
        self.assertEqual(self.telegram.answers, [listener.STALE])

    def test_foreign_chat_is_refused(self):
        run = self.handle(press(TOP_WEEK, chat_id="999"))
        run.assert_not_awaited()
        self.assertEqual(self.telegram.answers, [listener.FOREIGN])

    def test_commands_work_too(self):
        run = self.handle(typed("/month"))
        self.assertEqual(run.await_args.args[1], "month")
        self.assertTrue(self.telegram.messages)

    def test_help_answers_without_running(self):
        run = self.handle(typed("/help"))
        run.assert_not_awaited()
        self.assertIn("/week", self.telegram.messages[0][0])

    def test_plain_text_is_ignored(self):
        run = self.handle(typed("привет"))
        run.assert_not_awaited()
        self.assertEqual(self.telegram.messages, [])

    def test_second_press_while_busy_is_dropped(self):
        """Топ — это десяток сообщений; два одновременных забьют чат."""
        lock = asyncio.Lock()

        async def scenario():
            await lock.acquire()   # как будто топ уже собирается
            with mock.patch.object(listener, "run_top", new=mock.AsyncMock()) as run:
                await listener.handle_update(self.config, self.telegram, press(TOP_WEEK), lock)
                return run

        run = asyncio.run(scenario())
        run.assert_not_awaited()
        self.assertEqual(self.telegram.answers, [listener.BUSY])

    def test_a_failing_top_does_not_kill_the_service(self):
        lock = asyncio.Lock()

        async def scenario():
            with mock.patch.object(
                listener, "run_top", new=mock.AsyncMock(side_effect=RuntimeError("Telegram молчит"))
            ):
                await listener.handle_update(self.config, self.telegram, press(TOP_WEEK), lock)

        asyncio.run(scenario())  # не должно бросить наружу
        self.assertFalse(lock.locked(), "замок должен освободиться после сбоя")


class AllowedTest(unittest.TestCase):
    def test_falls_back_to_the_single_chat(self):
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                **{
                    **Config.from_env().__dict__,
                    "telegram_chat_id": "42",
                    "chats_path": Path(tmp) / "chats.txt",
                }
            )
            self.assertTrue(listener.allowed(config, "42"))
            self.assertFalse(listener.allowed(config, "77"))

    def test_reads_the_broadcast_list(self):
        for key in list(os.environ):
            if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                del os.environ[key]
        with tempfile.TemporaryDirectory() as tmp:
            chats = Path(tmp) / "chats.txt"
            chats.write_text("42\n77\n", encoding="utf-8")
            config = Config(
                **{**Config.from_env().__dict__, "telegram_chat_id": "42", "chats_path": chats}
            )
            self.assertTrue(listener.allowed(config, "77"))
            self.assertFalse(listener.allowed(config, "999"))


if __name__ == "__main__":
    unittest.main()
