"""Лента целиком: показ, отправка, память, границы слоёв."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from inspobot import feed as feedmod
from inspobot.config import Config
from inspobot.models import Block, Feed, Find
from inspobot.render import (
    feed_header_html,
    find_caption_html,
    find_keyboard,
    find_text_html,
    grouped,
)
from inspobot.sources import SECTION_BY_KEY, SourcesError
from inspobot.state import Store
from inspobot.telegram import TelegramError

DAY = date(2026, 9, 16)


def a_find(**changes) -> Find:
    base = dict(
        source="dribbble:product", origin="Product",
        url="https://dribbble.com/shots/1", title="Wallet onboarding",
        author="Ramotion", image="https://cdn.ru/1.png",
    )
    base.update(changes)
    return Find(**base)


def a_feed(*finds: Find, section: str = "dribbble") -> Feed:
    return Feed(day=DAY, blocks=(Block(SECTION_BY_KEY[section], finds or (a_find(),)),))


def a_config(tmp: Path, **changes) -> Config:
    base = dict(
        telegram_token="t", telegram_chat_id="1", anthropic_api_key="",
        anthropic_workspace_id="", model="m", effort="high", max_tokens=1,
        server_fallbacks=False, mobbin_mcp_url="", mobbin_token_file=tmp / "tok.json",
        mobbin_access_token="", db_path=tmp / "db.sqlite3", profile_path=tmp / "p.json",
        chats_path=tmp / "chats.txt", image_mode="document", top_buttons=False,
        timezone="Europe/Moscow", hour=11, minute=0,
        sources_path=tmp / "sources.json", studios_path=tmp / "studios.txt",
        feed_image_mode="photo", feed_shots=True, feed_shot_url="", feed_robots=True,
    )
    base.update(changes)
    return Config(**base)


class FakeTelegram:
    """Подставной Telegram: журнал вызовов и управляемые отказы."""

    def __init__(self, media_fails: bool = False, message_fails: bool = False):
        self.media_fails = media_fails
        self.message_fails = message_fails
        self.media: list[str] = []
        self.messages: list[str] = []

    async def pause(self) -> None:
        return None

    async def send_media(self, url, caption, *, as_document=False, keyboard=None, chat_id=None):
        if self.media_fails:
            return None
        self.media.append(url)
        return len(self.media)

    async def send_message(self, text, *, chat_id=None, keyboard=None):
        if self.message_fails:
            raise TelegramError("чат закрыт")
        self.messages.append(text)
        return {"message_id": len(self.messages)}


def run(coro):
    return asyncio.run(coro)


class HeaderTests(unittest.TestCase):
    def test_lists_every_section_with_counts(self):
        out = feed_header_html(a_feed(a_find(), a_find(url="https://d.ru/2")), remembered=5)
        # Между числом и месяцем неразрывный пробел — так же, как везде в боте.
        self.assertIn("Дайджест на 16\u00a0сентября", out)
        self.assertIn("🏀 Dribbble — Product, Web, Mobile — 2", out)
        self.assertIn("Новых ссылок: 5", out)

    def test_empty_feed_says_so_plainly(self):
        out = feed_header_html(Feed(day=DAY, blocks=()))
        self.assertIn("ни один источник ничего нового не отдал", out)
        self.assertNotIn("Новых ссылок", out)


class CaptionTests(unittest.TestCase):
    def test_short_section_name_plus_origin(self):
        block = a_feed().blocks[0]
        self.assertIn("🏀 Dribbble · Product · 1/6", find_caption_html(block, a_find(), 1, 6))

    def test_origin_equal_to_section_is_not_repeated(self):
        block = Block(SECTION_BY_KEY["tilda"], ())
        head = find_caption_html(block, a_find(origin="Made on Tilda"), 1, 3).splitlines()[0]
        self.assertEqual(head, "🧱 Made on Tilda · 1/3")

    def test_likes_use_a_nonbreaking_space(self):
        out = find_caption_html(a_feed().blocks[0], a_find(likes=1234), 1, 1)
        self.assertIn("♥ 1 234 лайка", out)

    def test_summary_repeating_the_title_is_dropped(self):
        """og:description у половины сайтов — это заголовок. Карточка не
        должна заикаться."""
        out = find_caption_html(
            a_feed().blocks[0], a_find(summary="Wallet onboarding"), 1, 1
        )
        self.assertEqual(out.count("Wallet onboarding"), 1)

    def test_html_in_source_text_is_escaped(self):
        out = find_caption_html(
            a_feed().blocks[0], a_find(title="<script>alert(1)</script>"), 1, 1
        )
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_url_without_a_title_still_labels_the_card(self):
        self.assertIn("https://a.ru/x", find_caption_html(
            a_feed().blocks[0], a_find(title="", author="", url="https://a.ru/x"), 1, 1
        ))

    def test_text_form_carries_the_link_because_buttons_do_not_survive_forwarding(self):
        out = find_text_html(a_feed().blocks[0], a_find(), 1, 1)
        self.assertIn('<a href="https://dribbble.com/shots/1">', out)

    def test_text_form_never_exceeds_the_message_limit(self):
        long = a_find(summary="я" * 5000, title="т" * 400)
        self.assertLessEqual(len(find_text_html(a_feed().blocks[0], long, 1, 1)), 4096)

    def test_keyboard_points_at_the_find(self):
        self.assertEqual(
            find_keyboard(a_find())["inline_keyboard"][0][0]["url"],
            "https://dribbble.com/shots/1",
        )

    def test_grouped_uses_nonbreaking_spaces(self):
        self.assertEqual(grouped(1234567), "1 234 567")


class SendTests(unittest.TestCase):
    def test_picture_goes_as_media(self):
        tg = FakeTelegram()
        block = a_feed().blocks[0]
        self.assertTrue(run(feedmod.send_find(tg, block, a_find(), 1, 1, None, False)))
        self.assertEqual(tg.media, ["https://cdn.ru/1.png"])
        self.assertEqual(tg.messages, [])

    def test_find_without_a_picture_goes_as_text(self):
        tg = FakeTelegram()
        block = a_feed().blocks[0]
        run(feedmod.send_find(tg, block, a_find(image=""), 1, 1, None, False))
        self.assertEqual(tg.media, [])
        self.assertIn("dribbble.com/shots/1", tg.messages[0])

    def test_unreachable_picture_falls_back_to_text_not_silence(self):
        """Чужая картинка может не отдаться. Молча пропустить находку —
        это раздел из четырёх кейсов, превратившийся в раздел из одного."""
        tg = FakeTelegram(media_fails=True)
        block = a_feed().blocks[0]
        self.assertTrue(run(feedmod.send_find(tg, block, a_find(), 1, 1, None, False)))
        self.assertEqual(len(tg.messages), 1)

    def test_a_chat_that_refuses_everything_reports_failure(self):
        tg = FakeTelegram(media_fails=True, message_fails=True)
        block = a_feed().blocks[0]
        self.assertFalse(run(feedmod.send_find(tg, block, a_find(), 1, 1, None, False)))

    def test_deliver_sends_header_then_every_find(self):
        tg = FakeTelegram()
        feed = a_feed(a_find(), a_find(url="https://dribbble.com/shots/2"))
        sent = run(feedmod.deliver(tg, feed, None, False, remembered=2))
        self.assertEqual(sent, 2)
        self.assertEqual(len(tg.messages), 1)
        self.assertEqual(len(tg.media), 2)

    def test_broadcast_survives_one_dead_chat(self):
        class OneChatFails(FakeTelegram):
            async def send_message(self, text, *, chat_id=None, keyboard=None):
                if chat_id == "bad":
                    raise TelegramError("чат закрыт")
                return await FakeTelegram.send_message(self, text, chat_id=chat_id)

        tg = OneChatFails()
        sent, failed = run(feedmod.broadcast(tg, a_feed(), ["good", "bad"], False))
        self.assertEqual(sent, 1)
        self.assertEqual(failed, ["bad"])


class MemoryTests(unittest.TestCase):
    def test_remember_normalizes_and_counts_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db.sqlite3")
            feed = a_feed(a_find(url="https://dribbble.com/shots/1?utm_source=tg"))
            self.assertEqual(feedmod.remember(store, feed), 1)
            self.assertIn("https://dribbble.com/shots/1", store.known_links())
            # Тот же кейс без меток — уже знакомый.
            self.assertEqual(feedmod.remember(store, a_feed(a_find())), 0)


class SourceSelectionTests(unittest.TestCase):
    def test_only_filters_by_section_or_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = a_config(Path(tmp))
            self.assertTrue(
                all(s.section == "dribbble" for s in feedmod.sources_for(config, ["dribbble"]))
            )
            self.assertEqual([s.key for s in feedmod.sources_for(config, ["cssda"])], ["cssda"])

    def test_only_with_no_match_is_an_error_not_an_empty_letter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SourcesError, "не подошёл ни один"):
                feedmod.sources_for(a_config(Path(tmp)), ["нетакого"])


class PullerWiringTests(unittest.TestCase):
    """Подключение сборщиков, которые ходят не по HTTP.

    Отдельный тест потому, что здесь уже была ошибка, которую не поймал ни
    один из остальных: функция оказалась `async def`, `gather` получил
    корутину вместо словаря и падал на `way in pullers`. Проверки на
    `build()` не было, и увидел это только живой запуск.
    """

    def test_it_is_a_plain_function_not_a_coroutine(self):
        self.assertFalse(asyncio.iscoroutinefunction(feedmod.pullers_for))

    def test_mobbin_source_gets_a_puller(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = a_config(Path(tmp))
            sources = feedmod.sources_for(config, ["mobbin"])
            got = feedmod.pullers_for(config, sources, DAY)
            self.assertEqual(list(got), ["mobbin"])
            self.assertTrue(asyncio.iscoroutinefunction(got["mobbin"]))

    def test_without_mobbin_nothing_is_imported_or_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = a_config(Path(tmp))
            sources = feedmod.sources_for(config, ["dribbble"])
            self.assertEqual(feedmod.pullers_for(config, sources, DAY), {})

    def test_the_way_name_matches_what_sources_declare(self):
        """Ключ словаря обязан совпасть со способом в объявлении источника —
        иначе gather молча пойдёт качать `mcp://` по HTTP."""
        with tempfile.TemporaryDirectory() as tmp:
            config = a_config(Path(tmp))
            sources = feedmod.sources_for(config, ["mobbin"])
            self.assertIn(
                list(feedmod.pullers_for(config, sources, DAY))[0], sources[0].ways
            )


class LayerTests(unittest.TestCase):
    def test_feed_runs_without_the_anthropic_package(self):
        """Обещание «лента не ходит к Клоду» проверяется импортом, а не
        обещанием: `anthropic` не должен появиться в sys.modules ни одним
        путём. Иначе на сервере всё равно понадобится пакет, и обещание
        окажется неправдой при первом же `pip install -r`.
        """
        code = (
            "import sys; import inspobot.feed; "
            "leaked = [m for m in ('anthropic', 'inspobot.curator', 'inspobot.daily') "
            "if m in sys.modules]; "
            "print(','.join(leaked))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(out.stdout.strip(), "", f"лента затянула лишнее: {out.stdout!r}")

    def test_harvest_touches_neither_network_nor_database(self):
        """Граница слоя: разбор обязан оставаться проверяемым без сети."""
        source = (Path(__file__).resolve().parent.parent / "inspobot" / "harvest.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("import httpx", "import sqlite3", "from .fetcher", "from .state"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
