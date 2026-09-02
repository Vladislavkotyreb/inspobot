import unittest
from datetime import date

from inspobot.models import Digest, Pick
from inspobot.render import CAPTION_LIMIT, caption_html, header_html, human_date
from inspobot.topics import topics_for

DAY = date(2026, 9, 2)
MOBILE, DESKTOP = topics_for(DAY)


def make_pick(platform="ios", **over):
    base = dict(
        platform=platform,
        screen_id="ed0c23e4-d2c7-428f-bf9c-d6efd38d7f47",
        mobbin_url="https://mobbin.com/screens/ed0c23e4-d2c7-428f-bf9c-d6efd38d7f47",
        image_url="https://mobbin.com/api/mcp/short/xBdpdLiz",
        app_name="Claude",
        pattern="Группировка настроек",
        note="Опасное действие вынесено из общего списка.",
    )
    base.update(over)
    return Pick(**base)


def make_digest(picks=None, summary="Сегодня про иерархию."):
    return Digest(
        day=DAY,
        mobile_topic=MOBILE,
        desktop_topic=DESKTOP,
        summary=summary,
        picks=tuple(picks or [make_pick(), make_pick("web")]),
    )


class HumanDateTest(unittest.TestCase):
    def test_genitive_month(self):
        self.assertEqual(human_date(date(2026, 9, 2)), "2\u00a0сентября")
        self.assertEqual(human_date(date(2026, 1, 31)), "31\u00a0января")
        self.assertEqual(human_date(date(2026, 12, 1)), "1\u00a0декабря")

    def test_number_and_month_do_not_break(self):
        """Между числом и месяцем — неразрывный пробел, а не обычный."""
        self.assertNotIn(" ", human_date(date(2026, 9, 2)))


class HeaderTest(unittest.TestCase):
    def test_contains_date_and_topics(self):
        html = header_html(make_digest())
        self.assertIn("2\u00a0сентября", html)
        self.assertIn(MOBILE.title, html)
        self.assertIn(DESKTOP.title, html)
        self.assertIn("Сегодня про иерархию.", html)

    def test_survives_empty_summary(self):
        html = header_html(make_digest(summary=""))
        self.assertNotIn("\n\n\n", html)


class CaptionTest(unittest.TestCase):
    def test_shape(self):
        html = caption_html(make_pick(), 1, 5)
        self.assertIn("1/5", html)
        self.assertIn("<b>Claude</b>", html)
        self.assertIn('href="https://mobbin.com/screens/', html)

    def test_escapes_markup_from_the_model(self):
        html = caption_html(make_pick(app_name="A & <b>B</b>", note="1 < 2"), 1, 1)
        self.assertIn("A &amp; &lt;b&gt;B&lt;/b&gt;", html)
        self.assertIn("1 &lt; 2", html)

    def test_respects_telegram_caption_limit(self):
        html = caption_html(make_pick(note="я" * 4000), 1, 1)
        self.assertLessEqual(len(html), CAPTION_LIMIT)

    def test_optional_fields_do_not_leave_blank_lines(self):
        html = caption_html(make_pick(pattern="", note=""), 2, 3)
        self.assertNotIn("\n\n", html)


if __name__ == "__main__":
    unittest.main()
