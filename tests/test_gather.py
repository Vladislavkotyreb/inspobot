"""Сборка ленты: очередь способов, память, ротация, склейка разделов.

Сети здесь нет — вместо неё подставной загрузчик с готовыми страницами.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date

from inspobot.fetcher import Page, shot_url
from inspobot.gather import (
    Trace,
    collect,
    drop_repeats,
    fresh,
    harvest_page,
    rotate,
    take,
)
from inspobot.harvest import RawItem
from inspobot.models import Block
from inspobot.sources import SECTION_BY_KEY, Source

DAY = date(2026, 9, 16)

LIST_PAGE = """
<a href="/works/one">Первый кейс</a>
<a href="/works/two">Второй кейс</a>
<a href="/works/three">Третий кейс</a>
"""

CASE_PAGE = """
<html><head>
<meta property="og:title" content="Настоящее название">
<meta property="og:image" content="https://cdn.ru/cover.png">
<meta property="og:description" content="Описание кейса">
</head></html>
"""

FEED_PAGE = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Из ленты</title><link>https://a.ru/works/rss-one</link></item>
</channel></rss>"""


class FakeFetcher:
    """Подставной загрузчик: карта «адрес → страница» и журнал обращений."""

    def __init__(self, pages: dict[str, Page]):
        self.pages = pages
        self.asked: list[str] = []

    async def get(self, url: str, headers=None) -> Page:
        self.asked.append(url)
        page = self.pages.get(url)
        if page is None:
            return Page(url=url, status=404, error="HTTP 404")
        return page


def html(url: str, body: str) -> Page:
    return Page(url=url, status=200, body=body, content_type="text/html")


def a_source(**changes) -> Source:
    base = dict(
        key="studio", section="studios", title="Студия",
        url="https://a.ru/works/", ways=("html",),
        link_re=r"a\.ru/works/[^\"'?#]+", limit=2,
    )
    base.update(changes)
    return Source(**base)


def run(coro):
    return asyncio.run(coro)


class FreshTests(unittest.TestCase):
    def test_drops_seen_and_duplicates_within_a_page(self):
        items = [
            RawItem(url="https://a.ru/works/one"),
            RawItem(url="https://a.ru/works/one?utm_source=x"),
            RawItem(url="https://a.ru/works/two"),
        ]
        got = fresh(items, frozenset({"https://a.ru/works/two"}))
        self.assertEqual([i.url for i in got], ["https://a.ru/works/one"])

    def test_empty_url_never_passes(self):
        self.assertEqual(fresh([RawItem(url="")], frozenset()), [])


class RotateTests(unittest.TestCase):
    def test_start_moves_with_the_day(self):
        sources = [a_source(key=str(i)) for i in range(3)]
        days = {tuple(s.key for s in rotate(sources, date(2026, 9, d))) for d in (14, 15, 16)}
        self.assertEqual(len(days), 3, "за три дня порядок обязан смениться трижды")

    def test_all_sources_survive_rotation(self):
        sources = [a_source(key=str(i)) for i in range(5)]
        got = rotate(sources, DAY)
        self.assertEqual(sorted(s.key for s in got), sorted(s.key for s in sources))

    def test_single_source_is_untouched(self):
        one = [a_source()]
        self.assertEqual(rotate(one, DAY), one)


class HarvestPageTests(unittest.TestCase):
    def test_dispatch_by_way(self):
        source = a_source()
        page = html("https://a.ru/works/", LIST_PAGE)
        self.assertEqual(len(harvest_page(source, "html", page)), 3)
        self.assertEqual(
            harvest_page(source, "rss", html("https://a.ru/feed", FEED_PAGE))[0].url,
            "https://a.ru/works/rss-one",
        )


class TakeTests(unittest.TestCase):
    def pages(self) -> dict[str, Page]:
        return {
            "https://a.ru/works/": html("https://a.ru/works/", LIST_PAGE),
            "https://a.ru/works/one": html("https://a.ru/works/one", CASE_PAGE),
            "https://a.ru/works/two": html("https://a.ru/works/two", CASE_PAGE),
            "https://a.ru/works/three": html("https://a.ru/works/three", CASE_PAGE),
        }

    def test_takes_up_to_limit_and_enriches(self):
        fetcher = FakeFetcher(self.pages())
        finds, traces = run(take(fetcher, a_source(), need=5, seen=frozenset()))
        self.assertEqual(len(finds), 2)
        self.assertEqual(finds[0].image, "https://cdn.ru/cover.png")
        self.assertEqual(finds[0].summary, "Описание кейса")
        self.assertEqual(finds[0].origin, "Студия")
        self.assertTrue(traces[0].kept)

    def test_list_title_beats_og_title(self):
        fetcher = FakeFetcher(self.pages())
        finds, _ = run(take(fetcher, a_source(), need=1, seen=frozenset()))
        self.assertEqual(finds[0].title, "Первый кейс")

    def test_need_caps_below_source_limit(self):
        fetcher = FakeFetcher(self.pages())
        finds, _ = run(take(fetcher, a_source(limit=3), need=1, seen=frozenset()))
        self.assertEqual(len(finds), 1)

    def test_nothing_to_do_makes_no_requests(self):
        fetcher = FakeFetcher(self.pages())
        finds, traces = run(take(fetcher, a_source(), need=0, seen=frozenset()))
        self.assertEqual((finds, traces), ([], []))
        self.assertEqual(fetcher.asked, [])

    def test_seen_links_are_skipped(self):
        fetcher = FakeFetcher(self.pages())
        finds, _ = run(
            take(fetcher, a_source(), need=5, seen=frozenset({"https://a.ru/works/one"}))
        )
        self.assertEqual(
            [f.url for f in finds], ["https://a.ru/works/two", "https://a.ru/works/three"]
        )

    def test_next_way_is_tried_when_the_first_fails(self):
        source = a_source(ways=("rss", "html"), way_urls={"rss": "https://a.ru/feed"})
        fetcher = FakeFetcher(self.pages())  # ленты в карте нет — будет 404
        finds, traces = run(take(fetcher, source, need=1, seen=frozenset()))
        self.assertEqual([t.way for t in traces], ["rss", "html"])
        self.assertEqual(traces[0].error, "HTTP 404")
        self.assertEqual(len(finds), 1)

    def test_first_way_that_returns_items_wins(self):
        source = a_source(ways=("rss", "html"), way_urls={"rss": "https://a.ru/feed"})
        pages = self.pages()
        pages["https://a.ru/feed"] = html("https://a.ru/feed", FEED_PAGE)
        fetcher = FakeFetcher(pages)
        finds, traces = run(take(fetcher, source, need=2, seen=frozenset()))
        self.assertEqual([t.way for t in traces], ["rss"])
        self.assertEqual([f.url for f in finds], ["https://a.ru/works/rss-one"])

    def test_dead_source_yields_nothing_and_says_why(self):
        fetcher = FakeFetcher({})
        finds, traces = run(take(fetcher, a_source(), need=2, seen=frozenset()))
        self.assertEqual(finds, [])
        self.assertEqual(traces[0].error, "HTTP 404")
        self.assertIn("✗", traces[0].line())

    def test_enrichment_failure_keeps_the_find(self):
        """Страница кейса не открылась — находка всё равно должна остаться:
        ссылка есть, а заголовок возьмётся из списка."""
        pages = {"https://a.ru/works/": html("https://a.ru/works/", LIST_PAGE)}
        fetcher = FakeFetcher(pages)
        finds, _ = run(take(fetcher, a_source(), need=2, seen=frozenset()))
        self.assertEqual(len(finds), 2)
        self.assertEqual(finds[0].title, "Первый кейс")
        self.assertEqual(finds[0].image, "")

    def test_shot_fills_a_missing_picture(self):
        pages = {"https://a.ru/works/": html("https://a.ru/works/", LIST_PAGE)}
        fetcher = FakeFetcher(pages)
        finds, _ = run(take(fetcher, a_source(shot=True), need=1, seen=frozenset()))
        self.assertEqual(finds[0].image, shot_url("https://a.ru/works/one"))

    def test_shot_never_overwrites_a_real_picture(self):
        fetcher = FakeFetcher(self.pages())
        finds, _ = run(take(fetcher, a_source(shot=True), need=1, seen=frozenset()))
        self.assertEqual(finds[0].image, "https://cdn.ru/cover.png")

    def test_enrichment_can_be_switched_off(self):
        fetcher = FakeFetcher(self.pages())
        finds, _ = run(
            take(fetcher, a_source(), need=1, seen=frozenset(), enrich_pages=False)
        )
        self.assertEqual(fetcher.asked, ["https://a.ru/works/"])
        self.assertEqual(finds[0].image, "")


class DropRepeatsTests(unittest.TestCase):
    def make(self, section: str, urls: list[str]) -> Block:
        from inspobot.models import Find

        return Block(
            SECTION_BY_KEY[section],
            tuple(Find(source=section, origin=section, url=u, title=u) for u in urls),
        )

    def test_earlier_section_keeps_the_link(self):
        blocks = [
            self.make("cssda", ["https://a.ru/1", "https://a.ru/2"]),
            self.make("tilda", ["https://a.ru/2?utm_source=x", "https://a.ru/3"]),
        ]
        got = drop_repeats(blocks)
        self.assertEqual([f.url for f in got[0].finds], ["https://a.ru/1", "https://a.ru/2"])
        self.assertEqual([f.url for f in got[1].finds], ["https://a.ru/3"])

    def test_section_emptied_by_dedup_disappears(self):
        blocks = [self.make("cssda", ["https://a.ru/1"]), self.make("tilda", ["https://a.ru/1"])]
        got = drop_repeats(blocks)
        self.assertEqual([b.spec.key for b in got], ["cssda"])


class CollectTests(unittest.TestCase):
    def test_fills_a_section_and_stops_early(self):
        """Набралось с первого источника — до второго дело не доходит, и это
        несделанный запрос каждое утро. Раздел cssda берёт три находки,
        страница даёт ровно три."""
        one = a_source(key="one", section="cssda", url="https://a.ru/works/", limit=4)
        # Лимит у обоих одинаковый: тогда неважно, кого поставит первым
        # ротация — первый же наполняет раздел целиком.
        two = a_source(key="two", section="cssda", url="https://b.ru/works/",
                       link_re=r"b\.ru/works/", limit=4)
        pages = {
            "https://a.ru/works/": html("https://a.ru/works/", LIST_PAGE),
            "https://b.ru/works/": html("https://b.ru/works/", LIST_PAGE),
        }
        fetcher = FakeFetcher(pages)
        feed, traces = run(
            collect([one, two], fetcher, DAY, enrich_pages=False, shots=False)
        )
        self.assertEqual(len(feed.blocks), 1)
        self.assertEqual(len(feed.finds), 3)
        # Кто именно оказался первым, решает ротация по дню, и проверять это
        # здесь нельзя. Проверяется то, ради чего ранняя остановка и нужна:
        # второй список сегодня не скачивался вовсе.
        self.assertEqual(len(fetcher.asked), 1, fetcher.asked)
        self.assertEqual(len(traces), 1)

    def test_second_source_backfills_when_the_first_is_dead(self):
        one = a_source(key="one", url="https://dead.ru/", link_re=r"dead\.ru/x")
        two = a_source(key="two", url="https://a.ru/works/")
        fetcher = FakeFetcher({"https://a.ru/works/": html("https://a.ru/works/", LIST_PAGE)})
        feed, _ = run(collect([one, two], fetcher, DAY, enrich_pages=False, shots=False))
        self.assertEqual(len(feed.finds), 2)

    def test_nothing_anywhere_is_an_empty_feed_not_an_error(self):
        fetcher = FakeFetcher({})
        feed, traces = run(collect([a_source()], fetcher, DAY))
        self.assertEqual(feed.blocks, ())
        self.assertEqual(feed.finds, ())
        self.assertTrue(traces[0].error)

    def test_no_sources_at_all(self):
        feed, traces = run(collect([], FakeFetcher({}), DAY))
        self.assertEqual((feed.blocks, traces), ((), ()))

    def test_sections_come_out_in_declared_order(self):
        cssda = a_source(key="c", section="cssda", url="https://a.ru/works/")
        tilda = a_source(key="t", section="tilda", url="https://b.ru/works/",
                         link_re=r"b\.ru/works/")
        pages = {
            "https://a.ru/works/": html("https://a.ru/works/", LIST_PAGE),
            "https://b.ru/works/": html("https://b.ru/works/", LIST_PAGE),
        }
        feed, _ = run(
            collect([tilda, cssda], FakeFetcher(pages), DAY, enrich_pages=False, shots=False)
        )
        self.assertEqual([b.spec.key for b in feed.blocks], ["cssda", "tilda"])


class TraceTests(unittest.TestCase):
    def test_line_reports_success(self):
        line = Trace("cssda", "html", "https://a.ru", 200, found=9, kept=3, sample="https://a.ru/1").line()
        self.assertIn("✓", line)
        self.assertIn("ссылок 9", line)
        self.assertIn("взято 3", line)

    def test_line_reports_failure(self):
        self.assertIn("✗ HTTP 500", Trace("x", "html", "https://a.ru", 500, error="HTTP 500").line())


if __name__ == "__main__":
    unittest.main()
