import unittest

from inspobot import catalog, wizard
from inspobot.wizard import Selection


class StepOrderTest(unittest.TestCase):
    def test_screens_ask_all_four(self):
        sel = Selection()
        self.assertEqual(wizard.next_step(sel), wizard.STEP_KIND)
        sel = wizard.with_value(sel, wizard.STEP_KIND, "s")
        self.assertEqual(wizard.next_step(sel), wizard.STEP_PLATFORM)
        sel = wizard.with_value(sel, wizard.STEP_PLATFORM, "i")
        self.assertEqual(wizard.next_step(sel), wizard.STEP_AUDIENCE)
        sel = wizard.with_value(sel, wizard.STEP_AUDIENCE, "b")
        self.assertEqual(wizard.next_step(sel), wizard.STEP_TOPIC)
        sel = wizard.with_value(sel, wizard.STEP_TOPIC, "dashboard")
        self.assertIsNone(wizard.next_step(sel))
        self.assertTrue(wizard.is_complete(sel))

    def test_sections_skip_platform(self):
        """У search_sections нет параметра platform — спрашивать нечего."""
        sel = wizard.with_value(Selection(), wizard.STEP_KIND, "w")
        self.assertEqual(wizard.next_step(sel), wizard.STEP_AUDIENCE)
        sel = wizard.with_value(sel, wizard.STEP_AUDIENCE, "b")
        sel = wizard.with_value(sel, wizard.STEP_TOPIC, "pricing")
        self.assertTrue(wizard.is_complete(sel))
        self.assertEqual(wizard.api_platform(sel), "web")


class BackTest(unittest.TestCase):
    def test_unwinds_one_field(self):
        sel = Selection("s", "i", "b", "dashboard")
        sel = wizard.back(sel)
        self.assertEqual(sel, Selection("s", "i", "b", ""))
        sel = wizard.back(sel)
        self.assertEqual(sel, Selection("s", "i", "", ""))
        sel = wizard.back(sel)
        self.assertEqual(sel, Selection("s", "", "", ""))
        sel = wizard.back(sel)
        self.assertEqual(sel, Selection())
        self.assertIsNone(wizard.back(sel))

    def test_sections_do_not_step_back_into_platform(self):
        sel = Selection("w", "", "c", "hero")
        sel = wizard.back(sel)
        self.assertEqual(sel, Selection("w", "", "c", ""))
        sel = wizard.back(sel)
        self.assertEqual(sel, Selection("w", "", "", ""))
        self.assertEqual(wizard.next_step(sel), wizard.STEP_AUDIENCE)


class CallbackTest(unittest.TestCase):
    def test_roundtrip(self):
        for sel in (
            Selection(),
            Selection("s"),
            Selection("s", "i"),
            Selection("f", "w", "b"),
            Selection("s", "i", "b", "dashboard"),
            Selection("w", "", "c", "hero"),
        ):
            self.assertEqual(wizard.decode(wizard.encode(sel)), sel, sel)

    def test_every_reachable_selection_fits_telegram_limit(self):
        """64 байта на callback_data — проверяем весь каталог, а не пример."""
        longest = 0
        for kind_option in catalog.KINDS:
            kind = kind_option.code
            platforms = ("",) if kind in catalog.KINDS_WITHOUT_PLATFORM else ("i", "w")
            for platform in platforms:
                for audience_option in catalog.AUDIENCES:
                    for topic in catalog.topics_for(kind, audience_option.code):
                        data = wizard.encode(
                            Selection(kind, platform, audience_option.code, topic.slug)
                        )
                        longest = max(longest, len(data.encode("utf-8")))
        self.assertLessEqual(longest, wizard.CALLBACK_LIMIT)

    def test_rejects_foreign_and_broken_data(self):
        for data in (
            "",
            "other|s|i|b|dashboard",
            "w|s|i|b",
            "w|s|i|b|dashboard|extra",
            "w|z|i|b|dashboard",          # неизвестный вид
            "w|s|x|b|dashboard",          # неизвестная платформа
            "w|s|i|z|dashboard",          # неизвестная аудитория
            "w|s|i|b|nosuchtopic",        # темы нет в каталоге
            "w|w|i|b|pricing",            # платформа у секций
            "w|-|-|-|dashboard",          # тема без вида и аудитории
        ):
            self.assertIsNone(wizard.decode(data), data)

    def test_topic_from_the_other_audience_is_rejected(self):
        self.assertIsNotNone(wizard.decode("w|s|i|b|dashboard"))
        self.assertIsNone(wizard.decode("w|s|i|c|dashboard"))


class PresentationTest(unittest.TestCase):
    def test_breadcrumb_grows_with_selection(self):
        self.assertEqual(wizard.breadcrumb(Selection()), "")
        self.assertEqual(wizard.breadcrumb(Selection("s")), "Экраны")
        self.assertEqual(
            wizard.breadcrumb(Selection("s", "i", "b", "dashboard")),
            "Экраны · Мобильные · B2B · Дашборд",
        )

    def test_audience_prompt_admits_there_is_no_such_filter(self):
        text = wizard.prompt(wizard.STEP_AUDIENCE, Selection("s", "i"))
        self.assertIn("нет такого фильтра", text)

    def test_options_match_catalog(self):
        sel = Selection("f", "i", "c")
        slugs = [o.code for o in wizard.options(wizard.STEP_TOPIC, sel)]
        self.assertEqual(slugs, [t.slug for t in catalog.FLOWS_B2C])


class CatalogTest(unittest.TestCase):
    def test_platform_mapping(self):
        self.assertEqual(wizard.api_platform(Selection("s", "i", "b", "dashboard")), "ios")
        self.assertEqual(wizard.api_platform(Selection("s", "w", "b", "dashboard")), "web")

    def test_slugs_unique_and_queries_english(self):
        for kind, by_audience in catalog.TOPICS.items():
            for audience, topics in by_audience.items():
                slugs = [t.slug for t in topics]
                self.assertEqual(len(slugs), len(set(slugs)), (kind, audience))
                for topic in topics:
                    self.assertTrue(topic.query.isascii(), topic.slug)
                    self.assertTrue(topic.title.strip(), topic.slug)

    def test_every_kind_and_audience_has_topics(self):
        for kind_option in catalog.KINDS:
            for audience_option in catalog.AUDIENCES:
                topics = catalog.topics_for(kind_option.code, audience_option.code)
                self.assertGreaterEqual(len(topics), 6, (kind_option, audience_option))


if __name__ == "__main__":
    unittest.main()
