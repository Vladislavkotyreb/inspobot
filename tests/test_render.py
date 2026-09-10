import unittest
from datetime import date

from inspobot.models import Digest, Pick, Section
from inspobot.profile import DEFAULT_PROFILE, Slot, plan_for_day
from inspobot.render import CAPTION_LIMIT, caption_html, header_html, human_date, section_icon

DAY = date(2026, 9, 10)
PLAN = plan_for_day(DEFAULT_PROFILE, DAY)


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


def make_section(index=0, picks=None):
    slot, topic = PLAN[index]
    return Section(slot=slot, topic=topic, picks=tuple(picks or [make_pick()]))


def make_digest(sections=None, summary="Сегодня про иерархию."):
    return Digest(
        day=DAY,
        summary=summary,
        sections=tuple(sections or [make_section(0), make_section(1)]),
    )


class HumanDateTest(unittest.TestCase):
    def test_genitive_month(self):
        self.assertEqual(human_date(date(2026, 9, 2)), "2 сентября")
        self.assertEqual(human_date(date(2026, 1, 31)), "31 января")

    def test_number_and_month_do_not_break(self):
        """Между числом и месяцем — неразрывный пробел, а не обычный."""
        self.assertNotIn(" ", human_date(date(2026, 9, 2)))


class IconTest(unittest.TestCase):
    def test_screens_by_platform_flows_and_sections_by_kind(self):
        screens_ios = Section(Slot("s", "i", "c"), PLAN[0][1], ())
        screens_web = Section(Slot("s", "w", "b"), PLAN[1][1], ())
        flows = Section(Slot("f", "i", "c"), PLAN[2][1], ())
        sections = Section(Slot("w", "", "b"), PLAN[2][1], ())
        icons = {
            section_icon(screens_ios),
            section_icon(screens_web),
            section_icon(flows),
            section_icon(sections),
        }
        self.assertEqual(len(icons), 4, "иконки блоков должны различаться")


class HeaderTest(unittest.TestCase):
    def test_lists_every_block_with_its_count(self):
        digest = make_digest()
        html = header_html(digest)
        self.assertIn("10 сентября", html)
        for section in digest.sections:
            self.assertIn(section.title(), html)
        self.assertIn("— 1", html)
        self.assertIn("Сегодня про иерархию.", html)

    def test_survives_empty_summary(self):
        self.assertNotIn("\n\n\n", header_html(make_digest(summary="")))


class CaptionTest(unittest.TestCase):
    def test_shape(self):
        section = make_section(0)
        html = caption_html(section, section.picks[0], 1, 3)
        self.assertIn("1/3", html)
        self.assertIn(section.topic.title, html)
        self.assertIn("<b>Claude</b>", html)
        self.assertIn('href="https://mobbin.com/screens/', html)

    def test_escapes_markup_from_the_model(self):
        section = make_section(0, [make_pick(app_name="A & <b>B</b>", note="1 < 2")])
        html = caption_html(section, section.picks[0], 1, 1)
        self.assertIn("A &amp; &lt;b&gt;B&lt;/b&gt;", html)
        self.assertIn("1 &lt; 2", html)

    def test_respects_telegram_caption_limit(self):
        section = make_section(0, [make_pick(note="я" * 4000)])
        self.assertLessEqual(len(caption_html(section, section.picks[0], 1, 1)), CAPTION_LIMIT)

    def test_optional_fields_do_not_leave_blank_lines(self):
        section = make_section(0, [make_pick(pattern="", note="")])
        self.assertNotIn("\n\n", caption_html(section, section.picks[0], 2, 3))


if __name__ == "__main__":
    unittest.main()
