import unittest

from inspobot import catalog, render, wizard
from inspobot.wizard import Selection


class KeyboardTest(unittest.TestCase):
    def rows(self, step, selection):
        return render.wizard_keyboard(step, selection)["inline_keyboard"]

    def test_first_step_has_no_back_button(self):
        rows = self.rows(wizard.STEP_KIND, Selection())
        self.assertEqual(len(rows), 1)
        self.assertEqual([b["text"] for b in rows[0]], ["Экраны", "Флоу", "Секции сайта"])

    def test_back_button_appears_after_first_choice(self):
        rows = self.rows(wizard.STEP_PLATFORM, Selection("s"))
        self.assertEqual(rows[-1][0]["text"], render.BACK_LABEL)
        self.assertEqual(wizard.decode(rows[-1][0]["callback_data"]), Selection())

    def test_topics_are_two_per_row(self):
        rows = self.rows(wizard.STEP_TOPIC, Selection("s", "i", "b"))
        topic_rows = rows[:-1]  # последняя строка — «назад»
        self.assertTrue(all(len(r) <= render.TOPICS_PER_ROW for r in topic_rows))
        buttons = sum(len(r) for r in topic_rows)
        self.assertEqual(buttons, len(catalog.SCREENS_B2B))

    def test_every_button_decodes_back(self):
        """Каждая кнопка должна давать состояние, которое бот поймёт."""
        for step, selection in (
            (wizard.STEP_KIND, Selection()),
            (wizard.STEP_PLATFORM, Selection("f")),
            (wizard.STEP_AUDIENCE, Selection("w")),
            (wizard.STEP_TOPIC, Selection("w", "", "c")),
            (wizard.STEP_TOPIC, Selection("f", "i", "b")),
        ):
            for row in self.rows(step, selection):
                for button in row:
                    self.assertIsNotNone(
                        wizard.decode(button["callback_data"]), button
                    )

    def test_sections_keyboard_never_offers_platform(self):
        rows = self.rows(wizard.STEP_AUDIENCE, Selection("w"))
        labels = [b["text"] for row in rows for b in row]
        self.assertNotIn("Мобильные", labels)
        self.assertNotIn("Десктоп", labels)


class WizardTextTest(unittest.TestCase):
    def test_shows_breadcrumb_and_question(self):
        text = render.wizard_text(wizard.STEP_TOPIC, Selection("s", "i", "b"))
        self.assertIn("Экраны · Мобильные · B2B", text)
        self.assertIn("Тема?", text)

    def test_first_step_has_no_breadcrumb(self):
        text = render.wizard_text(wizard.STEP_KIND, Selection())
        self.assertTrue(text.startswith("Что ищем?"))

    def test_header_reports_how_many_found(self):
        text = render.selection_header(Selection("f", "i", "c", "purchase"), "Итог.", 4)
        self.assertIn("Нашлось: 4", text)
        self.assertIn("Итог.", text)


if __name__ == "__main__":
    unittest.main()
