"""Раздел Mobbin в ленте: тема недели, исключения, разбор ответа."""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import date
from pathlib import Path

import httpx

from inspobot.gather import collect, take
from inspobot.mcp_client import PROTOCOL, McpError
from inspobot.mobbin_feed import (
    MAX_EXCLUDE,
    MODE,
    THEMES,
    Theme,
    fresh_token,
    puller,
    screen_ids,
    theme_for,
    to_items,
)
from inspobot.sources import Source

DAY = date(2026, 9, 16)
UUID = "355c903b-4580-4f82-bd8f-0c46195fa434"

# Ответ снят с живого инструмента: полей ровно столько, и ни лайков, ни даты
# среди них нет.
PAYLOAD = {
    "query": "...",
    "screens": [
        {
            "id": UUID,
            "image_url": "https://mobbin.com/api/mcp/short/hC7fGuCI",
            "mobbin_url": f"https://mobbin.com/screens/{UUID}",
            "app_name": "Jomo",
            "platform": "ios",
        },
        {
            "id": "db5cc1d0-9d46-4b3f-b8b9-facb66175670",
            "image_url": "https://mobbin.com/api/mcp/short/0VWFeMwl",
            "mobbin_url": "https://mobbin.com/screens/db5cc1d0-9d46-4b3f-b8b9-facb66175670",
            "app_name": "Givingli",
            "platform": "ios",
        },
    ],
}


def run(coro):
    return asyncio.run(coro)


def a_source(**changes) -> Source:
    base = dict(
        key="mobbin", section="mobbin", title="Тема недели",
        url="mcp://mobbin/search_screens", ways=("mobbin",), limit=3,
        enrich=False, origin_from_tag=True,
    )
    base.update(changes)
    return Source(**base)


class ThemeTests(unittest.TestCase):
    def test_one_theme_per_week_not_per_day(self):
        """«Тема недели», меняющаяся каждый день, — неправда в заголовке."""
        week = [date(2026, 9, 14) , date(2026, 9, 17), date(2026, 9, 20)]
        self.assertEqual(len({theme_for(d).title for d in week}), 1)

    def test_next_week_is_a_different_theme(self):
        self.assertNotEqual(theme_for(date(2026, 9, 16)), theme_for(date(2026, 9, 23)))

    def test_the_whole_list_gets_used(self):
        seen = {theme_for(date(2026, 1, 5) + __import__("datetime").timedelta(weeks=w)).title
                for w in range(len(THEMES))}
        self.assertEqual(len(seen), len(THEMES))

    def test_every_theme_is_usable(self):
        for theme in THEMES:
            with self.subTest(theme.title):
                self.assertLessEqual(len(theme.query), 500, "предел Mobbin на запрос")
                self.assertIn(theme.platform, ("ios", "web"))
                self.assertTrue(theme.title.strip())

    def test_titles_are_unique(self):
        self.assertEqual(len({t.title for t in THEMES}), len(THEMES))


class ExcludeTests(unittest.TestCase):
    def test_pulls_ids_back_out_of_remembered_links(self):
        seen = [f"https://mobbin.com/screens/{UUID}", "https://dribbble.com/shots/1"]
        self.assertEqual(screen_ids(seen), [UUID])

    def test_uppercase_is_normalized(self):
        self.assertEqual(screen_ids([f"https://mobbin.com/screens/{UUID.upper()}"]), [UUID])

    def test_capped_at_the_tool_limit(self):
        """Сотня — предел инструмента. Лишнее он не проигнорирует, а отвергнет
        весь запрос."""
        many = [f"https://mobbin.com/screens/{UUID[:-1]}{i:x}" for i in range(16)] * 20
        self.assertEqual(len(screen_ids(many)), MAX_EXCLUDE)


class ToItemsTests(unittest.TestCase):
    def test_maps_the_live_shape(self):
        theme = Theme("Банки", "q")
        items = to_items(PAYLOAD, theme)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, f"https://mobbin.com/screens/{UUID}")
        self.assertEqual(items[0].title, "Jomo")
        self.assertEqual(items[0].image, "https://mobbin.com/api/mcp/short/hC7fGuCI")
        self.assertEqual(items[0].tags, ("Банки",))

    def test_no_likes_and_no_date_because_mobbin_returns_neither(self):
        items = to_items(PAYLOAD, Theme("Т", "q"))
        self.assertEqual(items[0].likes, 0)
        self.assertIsNone(items[0].published)

    def test_row_without_a_link_is_skipped(self):
        payload = {"screens": [{"id": "x", "app_name": "Без ссылки"}]}
        self.assertEqual(to_items(payload, Theme("Т", "q")), ())

    def test_junk_payload_is_empty(self):
        for junk in ("строка", None, [], {"screens": "не список"}):
            self.assertEqual(to_items(junk, Theme("Т", "q")), ())


class TokenTests(unittest.TestCase):
    def test_manual_token_cannot_be_refreshed_and_says_so(self):
        with self.assertRaisesRegex(McpError, "MOBBIN_ACCESS_TOKEN"):
            fresh_token(Path("/нет"), "https://api.mobbin.com/mcp", "ручной-токен")

    def test_missing_token_file_points_at_the_login_command(self):
        with self.assertRaisesRegex(McpError, "auth_cli"):
            fresh_token(Path("/нет/такого.json"), "https://api.mobbin.com/mcp")


class PullTests(unittest.TestCase):
    def transport(self, *, fail_first: bool = False):
        calls: list[dict] = []
        state = {"tries": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"],
                          "result": {"protocolVersion": PROTOCOL}},
                    headers={"mcp-session-id": "s1"},
                )
            if "id" not in body:
                return httpx.Response(202)
            calls.append(body["params"])
            if fail_first:
                state["tries"] += 1
                if state["tries"] == 1:
                    return httpx.Response(401, json={"error": {"message": "истёк"}})
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"],
                      "result": {"content": [{"type": "text", "text": json.dumps(PAYLOAD)}]}},
            )

        return httpx.MockTransport(handler), calls

    def pull(self, **kwargs):
        transport, calls = self.transport(**kwargs)
        return puller(
            "https://api.mobbin.com/mcp",
            Path("/нет/токена.json"),
            "",
            DAY,
            get_token=lambda: "tok",
            transport=transport,
        ), calls

    def test_arguments_match_the_tool_schema(self):
        pull, calls = self.pull()
        items = run(pull(a_source(), 3, frozenset()))
        self.assertEqual(len(items), 2)
        sent = calls[0]["arguments"]
        self.assertEqual(sent["mode"], MODE)
        self.assertEqual(sent["platform"], theme_for(DAY).platform)
        self.assertEqual(sent["query"], theme_for(DAY).query)
        self.assertLessEqual(sent["limit"], 30)
        self.assertEqual(calls[0]["name"], "search_screens")

    def test_seen_links_become_exclude_ids(self):
        pull, calls = self.pull()
        run(pull(a_source(), 3, frozenset({f"https://mobbin.com/screens/{UUID}"})))
        self.assertEqual(calls[0]["arguments"]["exclude_screen_ids"], [UUID])

    def test_no_memory_means_no_exclude_key_at_all(self):
        pull, calls = self.pull()
        run(pull(a_source(), 3, frozenset()))
        self.assertNotIn("exclude_screen_ids", calls[0]["arguments"])

    def test_expired_token_is_refreshed_once(self):
        """401 посреди прогона случается: токен живёт час. Повтор ровно один,
        иначе на протухшем refresh-токене получается бесконечный круг."""
        transport, calls = self.transport(fail_first=True)
        tokens = iter(["tok-old", "tok-new"])
        pull = puller(
            "https://api.mobbin.com/mcp", Path("/нет.json"), "", DAY,
            get_token=lambda: next(tokens), transport=transport,
        )
        # Обновление уходит в fresh_token, а файла с токеном нет — значит
        # повтор состоялся и упал именно там, а не зациклился.
        with self.assertRaisesRegex(McpError, "auth_cli"):
            run(pull(a_source(), 3, frozenset()))
        self.assertEqual(len(calls), 1)

    def test_section_reaches_the_digest_through_gather(self):
        pull, _ = self.pull()
        finds, traces = run(
            take(None, a_source(), need=3, seen=frozenset(), pullers={"mobbin": pull})
        )
        self.assertEqual([f.title for f in finds], ["Jomo", "Givingli"])
        # Подпись берётся из темы недели, а не из неизменного title источника.
        self.assertEqual(finds[0].origin, theme_for(DAY).title)
        self.assertTrue(finds[0].image)
        self.assertEqual(traces[0].way, "mobbin")
        self.assertEqual(traces[0].kept, 2)

    def test_a_broken_mobbin_does_not_take_the_letter_down(self):
        async def angry(source, need, seen):
            raise McpError("сервер прилёг")

        feed, traces = run(
            collect([a_source()], None, DAY, pullers={"mobbin": angry})
        )
        self.assertEqual(feed.blocks, ())
        self.assertIn("сервер прилёг", traces[0].error)


if __name__ == "__main__":
    unittest.main()
