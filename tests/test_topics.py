import unittest
from datetime import date, timedelta

from inspobot.topics import DESKTOP_TOPICS, MOBILE_TOPICS, topics_for


class TopicRotationTest(unittest.TestCase):
    def test_deterministic(self):
        day = date(2026, 9, 2)
        self.assertEqual(topics_for(day), topics_for(day))

    def test_no_repeat_within_cycle(self):
        start = date(2026, 1, 1)
        mobile = [topics_for(start + timedelta(days=i))[0].slug for i in range(len(MOBILE_TOPICS))]
        desktop = [topics_for(start + timedelta(days=i))[1].slug for i in range(len(DESKTOP_TOPICS))]
        self.assertEqual(len(set(mobile)), len(MOBILE_TOPICS))
        self.assertEqual(len(set(desktop)), len(DESKTOP_TOPICS))

    def test_cycle_repeats_after_full_pass(self):
        day = date(2026, 3, 10)
        later = day + timedelta(days=len(MOBILE_TOPICS))
        self.assertEqual(topics_for(day)[0], topics_for(later)[0])

    def test_pairs_are_not_locked_together(self):
        """Разная длина списков: пара тем не повторяется каждый мобильный цикл."""
        self.assertNotEqual(len(MOBILE_TOPICS), len(DESKTOP_TOPICS))
        start = date(2026, 1, 1)
        span = len(MOBILE_TOPICS) * 3
        pairs = {
            (topics_for(start + timedelta(days=i))[0].slug,
             topics_for(start + timedelta(days=i))[1].slug)
            for i in range(span)
        }
        self.assertEqual(len(pairs), span)

    def test_queries_are_english_and_non_empty(self):
        for topic in MOBILE_TOPICS + DESKTOP_TOPICS:
            self.assertTrue(topic.query.strip(), topic.slug)
            self.assertTrue(topic.title.strip(), topic.slug)
            self.assertTrue(topic.query.isascii(), topic.slug)


if __name__ == "__main__":
    unittest.main()
