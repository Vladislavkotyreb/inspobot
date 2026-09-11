"""Топ за период: без Claude, без Mobbin — только база и копии сообщений."""

import asyncio
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from inspobot import top
from inspobot.config import Config
from inspobot.state import Store
from tests.test_daily import FakeTelegram

TODAY = date(2026, 9, 11)


def add(store, day, i, score, kind="s", app=None):
    sid = f"{i:08d}-0000-4000-8000-000000000000"
    return store.record_pick(
        day=day, kind=kind, platform="ios", screen_id=sid, app_name=app or f"App {i}",
        pattern="Приём", note="Заметка.", mobbin_url=f"https://mobbin.com/screens/{sid}",
        topic="Тема", block="Экраны · Мобильные · B2C", score=score,
    )


class PeriodTest(unittest.TestCase):
    def test_week_is_seven_days_including_today(self):
        since, until = top.period_bounds("week", TODAY)
        self.assertEqual((until - since).days + 1, 7)
        self.assertEqual(until, TODAY)

    def test_month_is_thirty_days(self):
        since, until = top.period_bounds("month", TODAY)
        self.assertEqual((until - since).days + 1, 30)


class RunTest(unittest.TestCase):
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
                "db_path": Path(self.dir.name) / "state.sqlite3",
                "chats_path": Path(self.dir.name) / "chats.txt",
            }
        )
        self.store = Store(self.config.db_path)

    def tearDown(self):
        self.dir.cleanup()

    def run_top(self, period="week", telegram=None, chat_id=None):
        telegram = telegram or FakeTelegram()
        with mock.patch.object(top, "Telegram", return_value=telegram):
            count = asyncio.run(top.run(self.config, period, chat_id=chat_id, today=TODAY))
        return count, telegram

    def test_copies_the_original_messages_in_score_order(self):
        low = add(self.store, TODAY, 1, score=4)
        high = add(self.store, TODAY - timedelta(days=2), 2, score=9)
        self.store.record_delivery(low, "42", [201])
        self.store.record_delivery(high, "42", [202])

        count, telegram = self.run_top()
        self.assertEqual(count, 2)
        self.assertEqual([mid for _, mid, _, _ in telegram.copies], [202, 201])
        first_caption = telegram.copies[0][2]
        self.assertIn("#1", first_caption)
        self.assertIn("9/10", first_caption)
        self.assertIsNotNone(telegram.copies[0][3])  # кнопка

    def test_nothing_older_than_the_window(self):
        add(self.store, TODAY - timedelta(days=10), 1, score=10)
        inside = add(self.store, TODAY - timedelta(days=3), 2, score=5)
        self.store.record_delivery(inside, "42", [7])
        count, telegram = self.run_top("week")
        self.assertEqual(count, 1)
        count, _ = self.run_top("month")
        self.assertEqual(count, 2)

    def test_flow_gets_a_fresh_caption_and_a_copied_gallery(self):
        flow = add(self.store, TODAY, 1, score=8, kind="f")
        self.store.record_delivery(flow, "42", [300, 301, 302, 303])
        _, telegram = self.run_top()
        # шапка + подпись флоу
        self.assertEqual(len(telegram.messages), 2)
        self.assertIsNotNone(telegram.messages[1][2])
        self.assertEqual(telegram.group_copies, [("42", [301, 302, 303])])
        self.assertEqual(telegram.copies, [])

    def test_new_subscriber_gets_text_cards(self):
        """В этот чат находка не уходила — копировать нечего, но карточка будет."""
        pick = add(self.store, TODAY, 1, score=8)
        self.store.record_delivery(pick, "42", [5])
        _, telegram = self.run_top(chat_id="777")
        self.assertEqual(telegram.copies, [])
        self.assertEqual(len(telegram.messages), 2)
        self.assertIn("App 1", telegram.messages[1][0])

    def test_one_app_once(self):
        a = add(self.store, TODAY, 1, score=9, app="Duolingo")
        b = add(self.store, TODAY, 2, score=8, app="duolingo")
        c = add(self.store, TODAY, 3, score=7, app="Notion")
        for pick_id in (a, b, c):
            self.store.record_delivery(pick_id, "42", [pick_id])
        count, _ = self.run_top()
        self.assertEqual(count, 2)

    def test_empty_period_sends_only_the_header(self):
        count, telegram = self.run_top()
        self.assertEqual(count, 0)
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("пуст", telegram.messages[0][0])

    def test_uses_no_api_clients(self):
        """Главное обещание: топ не стоит денег."""
        import inspobot.top as module

        self.assertFalse(hasattr(module, "collect"))
        self.assertFalse(hasattr(module, "make_client"))
        self.assertFalse(hasattr(module, "get_access_token"))


if __name__ == "__main__":
    unittest.main()
