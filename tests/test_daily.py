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


def make_pick(i, platform="ios", screens=(), score=7):
    sid = f"{i:08d}-0000-4000-8000-000000000000"
    return Pick(platform, sid, f"https://mobbin.com/screens/{sid}",
                f"https://mobbin.com/api/mcp/short/{i}", f"App {i}", "Приём", "Заметка.",
                tuple(screens), score)


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
    def __init__(self, token=None, chat_id=None, media_ok=True, group_ok=True, failing=()):
        self.messages: list[tuple[str, str | None, dict | None]] = []
        self.media: list[tuple[str, str | None, bool, dict | None]] = []
        self.groups: list[tuple[list[str], str, bool]] = []
        self.copies: list[tuple[str, int, str | None, dict | None]] = []
        self.group_copies: list[tuple[str, list[int]]] = []
        self.media_ok = media_ok
        self.group_ok = group_ok
        self.failing = set(failing)
        self._next_id = 100

    def _mid(self) -> int:
        self._next_id += 1
        return self._next_id

    @property
    def photos(self):
        return [(url, chat) for url, chat, _, _ in self.media]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _guard(self, chat_id):
        if chat_id in self.failing:
            raise TelegramError("Forbidden: bot was blocked by the user")

    async def send_message(self, text, chat_id=None, keyboard=None):
        self._guard(chat_id)
        self.messages.append((text, chat_id, keyboard))
        return {"message_id": self._mid()}

    async def send_media(
        self, image_url, caption, *, as_document=False, keyboard=None, chat_id=None
    ):
        self._guard(chat_id)
        if not self.media_ok:
            return None
        self.media.append((image_url, chat_id, as_document, keyboard))
        return self._mid()

    async def send_media_group(self, urls, caption="", *, as_document=False, chat_id=None):
        self._guard(chat_id)
        if not self.group_ok:
            return []
        self.groups.append((list(urls), caption, as_document))
        return [self._mid() for _ in urls]

    async def copy_message(self, from_chat_id, message_id, *, to_chat_id, caption=None, keyboard=None):
        self._guard(to_chat_id)
        self.copies.append((to_chat_id, message_id, caption, keyboard))
        return True

    async def copy_messages(self, from_chat_id, message_ids, *, to_chat_id):
        self._guard(to_chat_id)
        self.group_copies.append((to_chat_id, list(message_ids)))
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

    def test_sends_header_files_and_a_gallery_for_the_flow(self):
        telegram = FakeTelegram()
        sent, _ = self._run(telegram)
        self.assertEqual(sent, 4)
        self.assertEqual(len(telegram.media), 3)    # три обычных экрана
        self.assertEqual(len(telegram.groups), 1)   # флоу — галереей
        # шапка + подпись к флоу: к галерее кнопку прицепить нельзя
        self.assertEqual(len(telegram.messages), 2)

    def test_screens_go_as_uncompressed_files_with_a_button(self):
        telegram = FakeTelegram()
        self._run(telegram)
        for _, _, as_document, keyboard in telegram.media:
            self.assertTrue(as_document, "экраны должны уходить файлами, не фото")
            url = keyboard["inline_keyboard"][0][0]["url"]
            self.assertTrue(url.startswith("https://mobbin.com/"), url)

    def test_flow_caption_carries_the_button(self):
        telegram = FakeTelegram()
        self._run(telegram)
        _, _, keyboard = telegram.messages[-1]
        self.assertIsNotNone(keyboard)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Открыть на Mobbin")

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


class RecordingTest(RunOnceTest):
    def test_every_pick_is_stored_with_its_score(self):
        self._run(FakeTelegram())
        store = Store(self.config.db_path)
        top = store.top(MONDAY, MONDAY, limit=10)
        self.assertEqual(len(top), 4)
        self.assertTrue(all(p.score == 7 for p in top))

    def test_message_ids_are_remembered_per_chat(self):
        telegram = FakeTelegram()
        self._run(telegram)
        store = Store(self.config.db_path)
        picks = {p.screen_id: p for p in store.top(MONDAY, MONDAY, limit=10)}
        single = store.delivery(picks[make_pick(1).screen_id].id, "42")
        self.assertEqual(len(single), 1)
        flow = store.delivery(picks[make_pick(4).screen_id].id, "42")
        # подпись + пять шагов галереи
        self.assertEqual(len(flow), 6)

    def test_no_top_buttons_unless_enabled(self):
        telegram = FakeTelegram()
        self._run(telegram)
        header_keyboard = telegram.messages[0][2]
        self.assertIsNone(header_keyboard)

    def test_top_buttons_when_enabled(self):
        """Клавиатура постоянная, а не inline: inline уезжает вверх вместе с
        сообщением, а эта остаётся внизу чата."""
        from inspobot.render import MONTH_BUTTON, WEEK_BUTTON

        self.config = Config(**{**self.config.__dict__, "top_buttons": True})
        telegram = FakeTelegram()
        self._run(telegram)
        keyboard = telegram.messages[0][2]
        self.assertNotIn("inline_keyboard", keyboard)
        self.assertEqual(
            [b["text"] for b in keyboard["keyboard"][0]], [WEEK_BUTTON, MONTH_BUTTON]
        )
        self.assertTrue(keyboard["resize_keyboard"])


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
        self.assertEqual({chat for _, chat, _ in telegram.messages}, {"42", "77", "-100500"})

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
        self.assertEqual({chat for _, chat, _ in telegram.messages}, {"42"})

    def test_explicit_chat_overrides_the_list(self):
        self.write_chats("42", "77")
        telegram = FakeTelegram()
        self._run(telegram, chat_id="999")
        self.assertEqual({chat for _, chat, _ in telegram.messages}, {"999"})


class FlowDeliveryTest(unittest.TestCase):
    """Флоу показывается всеми шагами, а не одним превью."""

    def setUp(self):
        self.section = Section(PLAN[2][0], PLAN[2][1], ())

    def send(self, pick, telegram=None, as_document=False):
        telegram = telegram or FakeTelegram()
        asyncio.run(daily.send_pick(telegram, self.section, pick, 1, 1, None, as_document))
        return telegram

    def test_single_image_goes_as_one_file(self):
        telegram = self.send(make_pick(1))
        self.assertEqual(len(telegram.media), 1)
        self.assertEqual(telegram.groups, [])

    def test_flow_steps_go_as_one_gallery_after_the_caption(self):
        telegram = self.send(make_pick(1, screens=FLOW_STEPS[:6]))
        self.assertEqual(len(telegram.groups), 1)
        urls, caption, _ = telegram.groups[0]
        self.assertEqual(urls, list(FLOW_STEPS[:6]))
        self.assertEqual(caption, "", "подпись ушла отдельным сообщением с кнопкой")
        self.assertIn("App 1", telegram.messages[0][0])

    def test_long_flow_is_split_into_galleries(self):
        telegram = self.send(make_pick(1, screens=FLOW_STEPS))
        self.assertEqual([len(g[0]) for g in telegram.groups], [10, 4])
        self.assertEqual(len(telegram.messages), 1, "подпись одна на весь сценарий")

    def test_document_mode_is_passed_through(self):
        telegram = self.send(make_pick(1, screens=FLOW_STEPS[:3]), as_document=True)
        self.assertTrue(telegram.groups[0][2])

    def test_falls_back_to_single_files_when_gallery_fails(self):
        telegram = FakeTelegram(group_ok=False)
        self.send(make_pick(1, screens=FLOW_STEPS[:4]), telegram)
        self.assertEqual([url for url, _ in telegram.photos], list(FLOW_STEPS[:4]))
        self.assertEqual(len(telegram.messages), 1)  # подпись с кнопкой

    def test_button_survives_when_nothing_can_be_shown(self):
        telegram = FakeTelegram(media_ok=False, group_ok=False)
        self.send(make_pick(1, screens=FLOW_STEPS[:3]), telegram)
        self.assertEqual(len(telegram.messages), 1)
        text, _, keyboard = telegram.messages[0]
        self.assertNotIn("Открыть на Mobbin", text)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Открыть на Mobbin")

    def test_single_pick_falls_back_to_text_with_a_button(self):
        telegram = FakeTelegram(media_ok=False)
        self.send(make_pick(1), telegram)
        self.assertEqual(len(telegram.messages), 1)
        self.assertIsNotNone(telegram.messages[0][2])


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


class NetworkFailureTest(unittest.TestCase):
    """Сетевой сбой Telegram: у таймаутов httpx пустой текст, и сообщение
    об ошибке выходило пустым — «Ошибка:» и больше ничего."""

    def test_http_error_becomes_a_named_telegram_error(self):
        import asyncio as aio

        import httpx

        from inspobot.telegram import Telegram, TelegramError

        async def run():
            tg = Telegram("t", "42")
            with mock.patch.object(
                tg._client, "post", side_effect=httpx.ConnectTimeout("")
            ):
                with self.assertRaises(TelegramError) as caught:
                    await tg.send_message("привет")
            await tg.close()
            return str(caught.exception)

        message = aio.run(run())
        self.assertIn("sendMessage", message)
        self.assertIn("ConnectTimeout", message, "тип сбоя должен быть виден")

    def test_empty_message_exception_still_prints_its_type(self):
        import httpx

        exc = httpx.ConnectTimeout("")
        self.assertEqual(str(exc), "", "предпосылка теста: текст пустой")
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        self.assertEqual(detail, "ConnectTimeout")
