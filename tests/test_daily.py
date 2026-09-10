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
from inspobot.models import Digest, Pick, Section
from inspobot.profile import DEFAULT_PROFILE, Profile, Slot, plan_for_day, save
from inspobot.state import Store

DAY = date(2026, 9, 10)
PLAN = plan_for_day(DEFAULT_PROFILE, DAY)


def make_pick(i, platform="ios", screens=()):
    sid = f"{i:08d}-0000-4000-8000-000000000000"
    return Pick(platform, sid, f"https://mobbin.com/screens/{sid}",
                f"https://mobbin.com/api/mcp/short/{i}", f"App {i}", "Приём", "Заметка.",
                tuple(screens))


def make_digest():
    return Digest(
        day=DAY,
        summary="Итог дня.",
        sections=(
            Section(PLAN[0][0], PLAN[0][1], (make_pick(1), make_pick(2))),
            Section(PLAN[1][0], PLAN[1][1], (make_pick(3, "web"),)),
            Section(PLAN[2][0], PLAN[2][1], (make_pick(4),)),  # флоу
        ),
    )


class FakeTelegram:
    def __init__(self, token, chat_id, photo_ok=True, group_ok=True):
        self.messages: list[str] = []
        self.photos: list[str] = []
        self.groups: list[tuple[list[str], str, bool]] = []
        self.photo_ok = photo_ok
        self.group_ok = group_ok

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def send_message(self, text, chat_id=None):
        self.messages.append(text)
        return {}

    async def send_photo(self, image_url, caption, chat_id=None):
        if self.photo_ok:
            self.photos.append(image_url)
            return True
        return False

    async def send_media_group(self, urls, caption="", *, as_document=False, chat_id=None):
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
                "mobbin_access_token": "mobbin-token",
            }
        )
        self.digest = make_digest()

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
        self.assertEqual(sent, 4)
        self.assertEqual(len(telegram.photos), 4)
        self.assertEqual(len(telegram.messages), 1)  # только шапка
        self.assertIn("Итог дня.", telegram.messages[0])

    def test_header_lists_the_blocks(self):
        telegram = FakeTelegram("t", "42")
        self._run(telegram)
        for section in self.digest.sections:
            self.assertIn(section.title(), telegram.messages[0])

    def test_falls_back_to_text_when_photo_fails(self):
        telegram = FakeTelegram("t", "42", photo_ok=False)
        sent, _ = self._run(telegram)
        self.assertEqual(sent, 4)
        self.assertEqual(telegram.photos, [])
        self.assertEqual(len(telegram.messages), 5)  # шапка + четыре текстовых

    def test_second_run_same_day_is_skipped(self):
        self._run(FakeTelegram("t", "42"))
        telegram = FakeTelegram("t", "42")
        sent, collect = self._run(telegram)
        self.assertEqual(sent, 0)
        self.assertEqual(collect.call_count, 0)

    def test_only_screens_are_remembered_not_flows(self):
        self._run(FakeTelegram("t", "42"))
        store = Store(self.config.db_path)
        self.assertEqual(len(store.recent_seen_ids("ios")), 2)  # из блока экранов
        self.assertEqual(len(store.recent_seen_ids("web")), 1)

    def test_seen_ids_reach_the_next_request(self):
        self._run(FakeTelegram("t", "42"))
        _, collect = self._run(FakeTelegram("t", "42"), force=True)
        seen = collect.call_args.args[4]
        self.assertEqual(len(seen["ios"]), 2)
        self.assertEqual(len(seen["web"]), 1)

    def test_plan_comes_from_the_saved_profile(self):
        save(self.config.profile_path, Profile((Slot("w", "", "b", count=2),)))
        _, collect = self._run(FakeTelegram("t", "42"))
        plan = collect.call_args.args[2]
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0].kind, "w")

    def test_failure_is_reported_and_day_stays_open(self):
        telegram = FakeTelegram("t", "42")
        with mock.patch.object(daily, "collect", side_effect=RuntimeError("Mobbin молчит")), \
             mock.patch.object(daily, "Telegram", return_value=telegram):
            with self.assertRaises(RuntimeError):
                asyncio.run(daily.run_once(self.config, day=DAY))
        self.assertIn("Mobbin молчит", telegram.messages[0])
        self.assertFalse(Store(self.config.db_path).sent_today(DAY))

    def test_dry_run_sends_nothing(self):
        telegram = FakeTelegram("t", "42")
        sent, _ = self._run(telegram, dry_run=True)
        self.assertEqual(sent, 4)
        self.assertEqual(telegram.messages, [])
        self.assertFalse(Store(self.config.db_path).sent_today(DAY))


class ShowPlanTest(unittest.TestCase):
    def test_prints_plan_without_touching_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key in list(os.environ):
                if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
                    del os.environ[key]
            config = Config(
                **{**Config.from_env().__dict__, "profile_path": Path(tmp) / "profile.json"}
            )
            self.assertEqual(daily.show_plan(config, DAY), 0)


if __name__ == "__main__":
    unittest.main()


FLOW_STEPS = tuple(f"https://mobbin.com/api/mcp/step/{i}" for i in range(1, 15))


class FlowDeliveryTest(unittest.TestCase):
    """Флоу показывается всеми шагами, а не одним превью."""

    def setUp(self):
        self.section = Section(PLAN[2][0], PLAN[2][1], ())

    def send(self, pick, telegram=None, as_document=False):
        telegram = telegram or FakeTelegram("t", "42")
        asyncio.run(
            daily.send_pick(telegram, self.section, pick, 1, 1, None, as_document)
        )
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
        self.assertEqual(telegram.photos, [])

    def test_long_flow_is_split_and_captioned_once(self):
        """В галерею Telegram влезает десять картинок, шагов бывает больше."""
        telegram = self.send(make_pick(1, screens=FLOW_STEPS))
        self.assertEqual(len(telegram.groups), 2)
        self.assertEqual(len(telegram.groups[0][0]), 10)
        self.assertEqual(len(telegram.groups[1][0]), 4)
        self.assertNotEqual(telegram.groups[0][1], "")
        self.assertEqual(telegram.groups[1][1], "")

    def test_document_mode_is_passed_through(self):
        telegram = self.send(make_pick(1, screens=FLOW_STEPS[:3]), as_document=True)
        self.assertTrue(telegram.groups[0][2])

    def test_falls_back_to_single_photos_when_gallery_fails(self):
        telegram = FakeTelegram("t", "42", group_ok=False)
        self.send(make_pick(1, screens=FLOW_STEPS[:4]), telegram)
        self.assertEqual(telegram.photos, list(FLOW_STEPS[:4]))
        self.assertEqual(telegram.messages, [])

    def test_text_survives_when_nothing_can_be_shown(self):
        telegram = FakeTelegram("t", "42", photo_ok=False, group_ok=False)
        self.send(make_pick(1, screens=FLOW_STEPS[:3]), telegram)
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Открыть на Mobbin", telegram.messages[0])
