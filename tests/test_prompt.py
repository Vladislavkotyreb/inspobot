import json
import unittest
from datetime import date

from inspobot.prompt import CuratorError, build_messages, is_valid_pick, parse_digest
from inspobot.topics import topics_for

DAY = date(2026, 9, 2)
MOBILE, DESKTOP = topics_for(DAY)
UUID_A = "ed0c23e4-d2c7-428f-bf9c-d6efd38d7f47"
UUID_B = "2051c35c-fef5-4bd9-b8ad-a00c38ff9189"


def pick(screen_id=UUID_A, platform="ios", **over):
    base = {
        "platform": platform,
        "screen_id": screen_id,
        "mobbin_url": f"https://mobbin.com/screens/{screen_id}",
        "image_url": "https://mobbin.com/api/mcp/short/xBdpdLiz",
        "app_name": "Claude",
        "pattern": "Группировка настроек",
        "note": "Опасное действие вынесено из общего списка.",
    }
    base.update(over)
    return base


class BuildMessagesTest(unittest.TestCase):
    def test_carries_topics_and_platforms(self):
        text = build_messages(DAY, MOBILE, DESKTOP, 5)[0]["content"]
        self.assertIn(MOBILE.query, text)
        self.assertIn(DESKTOP.query, text)
        self.assertIn('platform="ios"', text)
        self.assertIn('platform="web"', text)

    def test_exclude_ids_only_when_present(self):
        without = build_messages(DAY, MOBILE, DESKTOP, 5)[0]["content"]
        self.assertNotIn("exclude_screen_ids", without)
        with_ids = build_messages(DAY, MOBILE, DESKTOP, 5, [UUID_A], [UUID_B])[0]["content"]
        self.assertIn(UUID_A, with_ids)
        self.assertIn(UUID_B, with_ids)


class ValidationTest(unittest.TestCase):
    def test_rejects_invented_data(self):
        self.assertTrue(is_valid_pick(pick()))
        self.assertFalse(is_valid_pick(pick(screen_id="not-a-uuid")))
        self.assertFalse(is_valid_pick(pick(mobbin_url="https://example.com/x")))
        self.assertFalse(is_valid_pick(pick(image_url="ftp://mobbin.com/x")))
        self.assertFalse(is_valid_pick(pick(platform="android")))


class ParseDigestTest(unittest.TestCase):
    def _parse(self, payload, seen=None):
        return parse_digest(json.dumps(payload), DAY, MOBILE, DESKTOP, seen)

    def test_happy_path(self):
        digest = self._parse({"summary": "Итог", "picks": [pick(), pick(UUID_B, "web")]})
        self.assertEqual(digest.summary, "Итог")
        self.assertEqual(len(digest.by_platform("ios")), 1)
        self.assertEqual(len(digest.by_platform("web")), 1)

    def test_drops_duplicates_and_already_seen(self):
        digest = self._parse(
            {"summary": "", "picks": [pick(), pick(), pick(UUID_B, "web")]},
        )
        self.assertEqual(len(digest.picks), 2)
        digest = self._parse(
            {"summary": "", "picks": [pick(), pick(UUID_B, "web")]}, seen={UUID_A}
        )
        self.assertEqual([p.screen_id for p in digest.picks], [UUID_B])

    def test_drops_invalid_entries(self):
        digest = self._parse(
            {"summary": "", "picks": [pick(), {"platform": "ios"}, pick(screen_id="x")]}
        )
        self.assertEqual(len(digest.picks), 1)

    def test_raises_when_nothing_usable(self):
        with self.assertRaises(CuratorError):
            self._parse({"summary": "", "picks": [{"platform": "ios"}]})
        with self.assertRaises(CuratorError):
            parse_digest("не json", DAY, MOBILE, DESKTOP)


if __name__ == "__main__":
    unittest.main()
