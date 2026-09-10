"""Сквозная проверка оркестровки без сети: Claude и Telegram подменены."""

import asyncio
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from inspobot import daily
from inspobot.config import Config
from inspobot.models import Digest, Pick, Section
from inspobot.profile import Day, Schedule, Slot, plan_for_day, save
from inspobot.state import Store
from inspobot.telegram import TelegramError

MONDAY = date(2026, 9, 14)
SATURDAY = MONDAY + timedelta(days=5)

SCHEDULE = Schedule(
    days=(
        Day(
            "Геймификация",
            (
                Slot("s", "i", "c", count=2),
                Slot("s", "w", "b", count=1),
                Slot("f", "i", "c", count=1),
            ),
        ),
    )
    + (None,) * 6
)
PLAN = plan_for_day(SCHEDULE, MONDAY)[1]
FLOW_STEPS = tuple(f"https://mobbin.com/api/mcp/step/{i}" for i in range(1, 15))


def make_pick(i, platform="ios", screens=()):
    sid = f"{i:08d}-0000-4000-8000-000000000000"
    return Pick(platform, sid, f"https://mobbin.com/screens/{sid}",
                f"https://mobbin.com/api/mcp/short/{i}", f"App {i}", "Приём", "Заметка.",
                tuple(screens))


def make_digest():
    return Digest(
        day=MONDAY,
        title="Геймификация",
        summary="Итог дня.",
        sections=(
            Section(PLAN[0][0], PLAN[0][1], (make_pick(1), make_pick(2))),
            Section(PLAN[1][0], PLAN[1][1], (make_pick(3, "web"),)),
            Section(PLAN[2][0], PLAN[2][1], (make_pick(4, screens=FLOW_STEPS[:5]),)),
        ),
    )


class FakeTelegram:
    def __init__(self, token=None, chat_id=None, photo_ok=True, group_ok=True, failing=()):
        self.messages: list[tuple[str, str | None]] = []
        self.photos: list[tuple[str, str | None]] = []
        self.groups: list[tuple[list[str], str, bool]] = []
        self.photo_ok = photo_ok
        self.group_ok = group_ok
        self.failing = set(failing)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _guard(self, chat_id):
        if chat_id in self.failing:
            raise TelegramError("Forbidden: bot was blocked by the user")

    async def send_message(self, text, chat_id=None):
        self._guard(chat_id)
        self.messages.append((text, chat_id))
        return {}

    async def send_photo(self, image_url, caption, chat_id=None):
        self._guard(chat_id)
        if not self.photo_ok:
            return False
        self.photos.append((image_url, chat_id))
        return True

    async def send_media_group(self, urls, caption="", *, as_document=False, chat_id=None):
        self._guard(chat_id)
        if not self.group_ok:
            return False
        self.groups.append((list(urls), caption, as_document))
        return True

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
                "profile_path": Path(self.dir.name) / "profile.json",
                "chats_path": Path(self.dir.name) / "chats.txt",
                "mobbin_access_token": "mobbin-token",
            }
        )
        save(self.config.profile_path, SCHEDULE)
        self.digest = make_digest()

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, telegram, day=MONDAY, **kwargs):
        with mock.patch.object(daily, "collect", return_value=self.digest) as collect, \
             mock.patch.object(daily, "Telegram", return_value=telegram):
            sent = asyncio.run(daily.run_once(self.config, day=day, **kwargs))
        return sent, collect

    def test_sends_header_photos_and_a_gallery_for_the_flow(self):
        telegram = FakeTelegram()
        sent, _ = self._run(telegram)
        self.assertEqual(sent, 4)
        self.assertEqual(len(telegram.photos), 3)   # три обычных экрана
        self.assertEqual(len(telegram.groups), 1)   # флоу — галереей
        self.assertEqual(len(telegram.messages), 1)  # шапка

    def test_day_off_sends_nothing_and_leaves_no_trace(self):
        telegram = FakeTelegram()
        sent, collect = self._run(telegram, day=SATURDAY)
        self.assertEqual(sent, 0)
        self.assertEqual(collect.call_count, 0)
        self.assertEqual(telegram.messages, [])
        self.assertFalse(Store(self.config.db_path).sent_today(SATURDAY))

    def test_day_title_reaches_the_model_call(self):
        _, collect = self._run(FakeTelegram())
        self.assertEqual(collect.call_args.args[5], "Геймификация")

    def test_second_run_same_day_is_skipped(self):
        self._run(FakeTelegram())
        sent, collect = self._run(FakeTelegram())
        self.assertEqual(sent, 0)
        self.assertEqual(collect.call_count, 0)

    def test_only_screens_are_remembered_not_flows(self):
        self._run(FakeTelegram())
        store = Store(self.config.db_path)
        self.assertEqual(len(store.recent_seen_ids("ios")), 2)
        self.assertEqual(len(store.recent_seen_ids("web")), 1)

    def test_failure_is_reported_and_day_stays_open(self):
        telegram = FakeTelegram()
        with mock.patch.object(daily, "collect", side_effect=RuntimeError("Mobbin молчит")), \
             mock.patch.object(daily, "Telegram", return_value=telegram):
            with self.assertRaises(RuntimeError):
                asyncio.run(daily.run_once(self.config, day=MONDAY))
        self.assertIn("Mobbin молчит", telegram.messages[0][0])
        self.assertFalse(Store(self.config.db_path).sent_today(MONDAY))


class BroadcastTest(RunOnceTest):
    def write_chats(self, *ids):
        self.config.chats_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.chats_path.write_text("\n".join(ids), encoding="utf-8")

    def test_one_digest_reaches_every_chat(self):
        self.write_chats("42", "77", "-100500")
        telegram = FakeTelegram()
        sent, collect = self._run(telegram)
        self.assertEqual(collect.call_count, 1, "модель должна вызываться один раз")
        self.assertEqual(sent, 12)  # четыре находки × три чата
        self.assertEqual({chat for _, chat in telegram.messages}, {"42", "77", "-100500"})

    def test_a_blocked_chat_does_not_stop_the_rest(self):
        self.write_chats("42", "77", "-100500")
        telegram = FakeTelegram(failing={"77"})
        sent, _ = self._run(telegram)
        self.assertEqual(sent, 8)
        self.assertEqual({chat for _, chat in telegram.photos}, {"42", "-100500"})
        self.assertTrue(Store(self.config.db_path).sent_today(MONDAY))

    def test_when_nobody_receives_the_run_fails(self):
        self.write_chats("42", "77")
        telegram = FakeTelegram(failing={"42", "77"})
        with self.assertRaises(TelegramError):
            self._run(telegram)
        self.assertFalse(Store(self.config.db_path).sent_today(MONDAY))

    def test_without_a_list_falls_back_to_the_single_chat(self):
        telegram = FakeTelegram()
        self._run(telegram)
        self.assertEqual({chat for _, chat in telegram.messages}, {"42"})

    def test_explicit_chat_overrides_the_list(self):
        self.write_chats("42", "77")
        telegram = FakeTelegram()
        self._run(telegram, chat_id="999")
        self.assertEqual({chat for _, chat in telegram.messages}, {"999"})


class FlowDeliveryTest(unittest.TestCase):
    """Флоу показывается всеми шагами, а не одним превью."""

    def setUp(self):
        self.section = Section(PLAN[2][0], PLAN[2][1], ())

    def send(self, pick, telegram=None, as_document=False):
        telegram = telegram or FakeTelegram()
        asyncio.run(daily.send_pick(telegram, self.section, pick, 1, 1, None, as_document))
        return telegram

    def test_single_image_goes_as_a_plain_photo(self):
        telegram = self.send(make_pick(1))
        self.assertEqual(len(telegram.photos), 1)
        self.assertEqual(telegram.groups, [])

    def test_flow_steps_go_as_one_gallery(self):
        telegram = self.send(make_pick(1, screens=FLOW_STEPS[:6]))
        self.assertEqual(len(telegram.groups), 1)
        urls, caption, _ = telegram.groups[0]
        self.assertEqual(urls, list(FLOW_STEPS[:6]))
        self.assertIn("App 1", caption)

    def test_long_flow_is_split_and_captioned_once(self):
        telegram = self.send(make_pick(1, screens=FLOW_STEPS))
        self.assertEqual([len(g[0]) for g in telegram.groups], [10, 4])
        self.assertNotEqual(telegram.groups[0][1], "")
        self.assertEqual(telegram.groups[1][1], "")

    def test_document_mode_is_passed_through(self):
        telegram = self.send(make_pick(1, screens=FLOW_STEPS[:3]), as_document=True)
        self.assertTrue(telegram.groups[0][2])

    def test_falls_back_to_single_photos_when_gallery_fails(self):
        telegram = FakeTelegram(group_ok=False)
        self.send(make_pick(1, screens=FLOW_STEPS[:4]), telegram)
        self.assertEqual([url for url, _ in telegram.photos], list(FLOW_STEPS[:4]))
        self.assertEqual(telegram.messages, [])

    def test_text_survives_when_nothing_can_be_shown(self):
        telegram = FakeTelegram(photo_ok=False, group_ok=False)
        self.send(make_pick(1, screens=FLOW_STEPS[:3]), telegram)
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Открыть на Mobbin", telegram.messages[0][0])


class ShowPlanTest(unittest.TestCase):
    def test_prints_the_week_without_touching_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key in list(os.environ):
                if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                    del os.environ[key]
            config = Config(
                **{
                    **Config.from_env().__dict__,
                    "profile_path": Path(tmp) / "profile.json",
                    "chats_path": Path(tmp) / "chats.txt",
                }
            )
            self.assertEqual(daily.show_plan(config, MONDAY), 0)


if __name__ == "__main__":
    unittest.main()
