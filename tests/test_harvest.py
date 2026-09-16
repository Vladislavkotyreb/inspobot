"""Разбор источников: только текст на входе, ничего из сети."""

from __future__ import annotations

import unittest
from datetime import date

from inspobot.harvest import (
    RawItem,
    absolutize,
    as_date,
    as_int,
    clean_text,
    dig,
    first,
    from_open_graph,
    link_key,
    open_graph,
    parse_feed,
    parse_json,
    parse_links,
)


class LinkKeyTests(unittest.TestCase):
    def test_strips_tracking_and_case(self):
        self.assertEqual(
            link_key("HTTPS://WWW.Dribbble.com/shots/123/?utm_source=tg&utm_campaign=x"),
            "https://dribbble.com/shots/123",
        )

    def test_keeps_meaningful_query_sorted(self):
        self.assertEqual(
            link_key("https://site.ru/case?b=2&a=1&fbclid=zz"),
            "https://site.ru/case?a=1&b=2",
        )

    def test_same_case_two_ways_collapses(self):
        """Ровно то, ради чего ключ существует: один кейс из ленты и со
        страницы-списка не должен прийти дважды."""
        self.assertEqual(
            link_key("http://www.site.ru/work/one/?utm_medium=rss#top"),
            link_key("https://site.ru/work/one"),
        )

    def test_fragment_never_survives(self):
        self.assertEqual(link_key("https://a.ru/x#gallery"), "https://a.ru/x")

    def test_root_keeps_slash(self):
        self.assertEqual(link_key("https://a.ru/"), "https://a.ru/")


class AbsolutizeTests(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(
            absolutize("https://a.ru/work/", "/case/1"), "https://a.ru/case/1"
        )

    def test_refuses_junk_schemes(self):
        for href in ("#top", "javascript:void(0)", "mailto:a@b.ru", "tel:+7", ""):
            self.assertEqual(absolutize("https://a.ru/", href), "", href)


class TextTests(unittest.TestCase):
    def test_strips_tags_scripts_and_entities(self):
        raw = "<div>Кейс <script>var x = '<b>';</script><b>&laquo;Пример&raquo;</b>  </div>"
        self.assertEqual(clean_text(raw), "Кейс «Пример»")

    def test_limit_cuts_on_word_boundary(self):
        out = clean_text("Очень длинное описание кейса про интерфейс", limit=20)
        self.assertLessEqual(len(out), 21)
        self.assertTrue(out.endswith("…"))
        self.assertNotIn("описани…", out)

    def test_likes_from_any_shape(self):
        self.assertEqual(as_int(1234), 1234)
        self.assertEqual(as_int("1 234"), 1234)
        self.assertEqual(as_int("1.2k"), 1200)
        self.assertEqual(as_int("3M"), 3000000)
        self.assertEqual(as_int("12,345"), 12345)
        self.assertEqual(as_int(None), 0)
        self.assertEqual(as_int("много"), 0)
        self.assertEqual(as_int(True), 0)

    def test_dates_from_any_shape(self):
        self.assertEqual(as_date("Tue, 15 Sep 2026 09:00:00 +0300"), date(2026, 9, 15))
        self.assertEqual(as_date("2026-09-15T10:00:00Z"), date(2026, 9, 15))
        self.assertEqual(as_date("опубликовано 2026-09-15"), date(2026, 9, 15))
        self.assertIsNone(as_date(""))
        self.assertIsNone(as_date("позавчера"))


class OpenGraphTests(unittest.TestCase):
    PAGE = """
    <html><head>
      <title>Запасной заголовок</title>
      <meta property="og:title" content="Кейс &laquo;Банк&raquo;" />
      <meta property="og:image" content="/static/cover.png">
      <meta property="og:image" content="/static/logo.png">
      <meta name="description" content="Как мы делали интерфейс">
      <meta name="twitter:creator" content="@studio">
    </head><body></body></html>
    """

    def test_first_image_wins(self):
        tags = open_graph(self.PAGE)
        self.assertEqual(tags["og:image"], "/static/cover.png")

    def test_fills_and_absolutizes(self):
        got = from_open_graph("https://a.ru/case/1", open_graph(self.PAGE))
        self.assertEqual(got["title"], "Кейс «Банк»")
        self.assertEqual(got["image"], "https://a.ru/static/cover.png")
        self.assertEqual(got["summary"], "Как мы делали интерфейс")

    def test_title_tag_is_the_fallback(self):
        tags = open_graph("<html><head><title>Только title</title></head></html>")
        self.assertEqual(from_open_graph("https://a.ru/", tags)["title"], "Только title")


class FilledTests(unittest.TestCase):
    def test_does_not_overwrite_known_fields(self):
        """Заголовок из списка точнее, чем og-слоган площадки."""
        item = RawItem(url="https://a.ru/1", title="Точное название")
        got = item.filled(title="Behance :: Photos, videos", image="https://a.ru/c.png")
        self.assertEqual(got.title, "Точное название")
        self.assertEqual(got.image, "https://a.ru/c.png")

    def test_no_changes_returns_same_object(self):
        item = RawItem(url="https://a.ru/1", title="Есть")
        self.assertIs(item.filled(title=""), item)


class ParseLinksTests(unittest.TestCase):
    PAGE = """
    <nav><a href="/about">О студии</a></nav>
    <a href="/works/bank-app">Банк&nbsp;— мобильный банк</a>
    <a href="https://studio.ru/works/bank-app?utm_source=main">то же самое</a>
    <a href="/works/shop">Магазин</a>
    <a href="/works/shop/attachments">вложения</a>
    <a href="https://vk.com/studio">ВК</a>
    """

    def test_filters_dedupes_and_keeps_order(self):
        items = parse_links(
            self.PAGE, base="https://studio.ru/", link_re=r"studio\.ru/works/[^\"'?#]+"
        )
        self.assertEqual(
            [i.url for i in items],
            [
                "https://studio.ru/works/bank-app",
                "https://studio.ru/works/shop",
                "https://studio.ru/works/shop/attachments",
            ],
        )

    def test_deny_wins(self):
        items = parse_links(
            self.PAGE,
            base="https://studio.ru/",
            link_re=r"studio\.ru/works/",
            deny_re=r"/attachments",
        )
        self.assertNotIn("https://studio.ru/works/shop/attachments", [i.url for i in items])

    def test_label_comes_from_link_text(self):
        items = parse_links(
            self.PAGE, base="https://studio.ru/", link_re=r"studio\.ru/works/bank"
        )
        self.assertEqual(items[0].title, "Банк — мобильный банк")

    def test_limit(self):
        items = parse_links(
            self.PAGE, base="https://studio.ru/", link_re=r"studio\.ru/works/", limit=1
        )
        self.assertEqual(len(items), 1)

    def test_outbound_only_rule_for_award_lists(self):
        """Правило Тильды: награду получает чужой сайт, а не сама Тильда."""
        page = '<a href="https://tilda.cc/pricing">Тарифы</a><a href="https://studio.io/">Сайт</a>'
        items = parse_links(
            page,
            base="https://tilda.cc/made-on-tilda/",
            link_re=r"^https?://(?!(?:[a-z0-9-]+\.)*tilda\.(?:cc|ws)/)[^/]+\.[a-z]{2,}",
        )
        self.assertEqual([i.url for i in items], ["https://studio.io/"])


class ParseFeedTests(unittest.TestCase):
    RSS = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel>
        <item>
          <title>Кейс: банк</title>
          <link>https://a.ru/case/1</link>
          <pubDate>Tue, 15 Sep 2026 09:00:00 +0300</pubDate>
          <dc:creator>Студия</dc:creator>
          <description>&lt;p&gt;&lt;img src="/img/cover.png"&gt;Текст&lt;/p&gt;</description>
          <category>ui</category>
        </item>
        <item>
          <title>С вложением</title>
          <link>https://a.ru/case/2</link>
          <enclosure url="https://cdn.ru/2.jpg" type="image/jpeg" length="1"/>
        </item>
        <item>
          <title>С media</title>
          <link>https://a.ru/case/3</link>
          <media:content url="https://cdn.ru/3.jpg" medium="image"/>
        </item>
      </channel>
    </rss>
    """

    ATOM = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Атом-запись</title>
        <link rel="edit" href="https://a.ru/api/edit/1"/>
        <link rel="alternate" href="https://a.ru/post/1"/>
        <published>2026-09-15T10:00:00Z</published>
        <author><name>Автор</name></author>
        <summary>Коротко</summary>
      </entry>
    </feed>
    """

    def test_rss_fields(self):
        items = parse_feed(self.RSS, "https://a.ru/")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].title, "Кейс: банк")
        self.assertEqual(items[0].author, "Студия")
        self.assertEqual(items[0].published, date(2026, 9, 15))
        self.assertEqual(items[0].tags, ("ui",))

    def test_image_sources_in_priority_order(self):
        items = parse_feed(self.RSS, "https://a.ru/")
        self.assertEqual(items[0].image, "https://a.ru/img/cover.png")
        self.assertEqual(items[1].image, "https://cdn.ru/2.jpg")
        self.assertEqual(items[2].image, "https://cdn.ru/3.jpg")

    def test_atom_prefers_alternate_link(self):
        items = parse_feed(self.ATOM, "https://a.ru/")
        self.assertEqual(items[0].url, "https://a.ru/post/1")
        self.assertEqual(items[0].author, "Автор")
        self.assertEqual(items[0].published, date(2026, 9, 15))

    def test_broken_xml_is_empty_not_an_exception(self):
        self.assertEqual(parse_feed("<rss><channel><item>", "https://a.ru/"), ())
        self.assertEqual(parse_feed("<html>это не лента</html>", "https://a.ru/"), ())
        self.assertEqual(parse_feed("", ""), ())

    def test_item_without_link_is_skipped(self):
        feed = "<rss><channel><item><title>Без ссылки</title></item></channel></rss>"
        self.assertEqual(parse_feed(feed, "https://a.ru/"), ())


class ParseJsonTests(unittest.TestCase):
    BODY = """
    {"data": {"projects": [
      {"id": 42, "slug": "bank", "name": "Банк",
       "owners": [{"display_name": "Студия"}],
       "covers": {"404": "https://cdn.ru/42.jpg"},
       "stats": {"appreciations": "1.2k"},
       "published_on": 1789000000},
      {"id": 43, "slug": "shop", "name": "Магазин", "covers": {}}
    ]}}
    """
    FIELDS = {
        "title": "name",
        "author": "owners.0.display_name",
        "image": "covers.original|covers.404",
        "likes": "stats.appreciations",
        "published": "published_on",
        "id": "id",
        "slug": "slug",
    }

    def test_maps_fields_and_builds_url(self):
        items = parse_json(
            self.BODY,
            "data.projects",
            self.FIELDS,
            url_template="https://www.behance.net/gallery/{id}/{slug}",
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://www.behance.net/gallery/42/bank")
        self.assertEqual(items[0].title, "Банк")
        self.assertEqual(items[0].author, "Студия")
        self.assertEqual(items[0].image, "https://cdn.ru/42.jpg")
        self.assertEqual(items[0].likes, 1200)

    def test_alternative_paths_survive_a_rename(self):
        """`covers.original` больше нет, есть `covers.404` — запись уцелела."""
        self.assertEqual(first({"covers": {"404": "x"}}, "covers.original|covers.404"), "x")

    def test_missing_pieces_do_not_raise(self):
        items = parse_json(
            self.BODY,
            "data.projects",
            self.FIELDS,
            url_template="https://www.behance.net/gallery/{id}/{slug}",
        )
        self.assertEqual(items[1].image, "")
        self.assertEqual(items[1].likes, 0)
        self.assertIsNone(items[1].published)

    def test_not_json_is_empty(self):
        self.assertEqual(parse_json("<html>", "a", {"title": "t"}), ())

    def test_wrong_path_is_empty(self):
        self.assertEqual(parse_json(self.BODY, "data.missing", {"title": "name"}), ())

    def test_dig_handles_indices_and_gaps(self):
        self.assertEqual(dig({"a": [{"b": 1}]}, "a.0.b"), 1)
        self.assertIsNone(dig({"a": []}, "a.0.b"))
        self.assertIsNone(dig({"a": 1}, "a.b"))
        self.assertIsNone(dig(None, "a"))


if __name__ == "__main__":
    unittest.main()
