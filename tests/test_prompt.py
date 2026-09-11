import json
import unittest
from datetime import date

from inspobot.prompt import (
    CuratorError,
    build_digest_messages,
    is_valid_pick,
    parse_digest,
    search_limit,
)
from inspobot.profile import Day, Schedule, Slot, plan_for_day

DAY = date(2026, 9, 14)  # понедельник
SCHEDULE = Schedule(
    days=(
        Day(
            "Тестовый день",
            (
                Slot("s", "i", "c", count=3),
                Slot("s", "w", "b", count=3),
                Slot("f", "i", "c", count=2),
            ),
        ),
    )
    + (None,) * 6
)
PLAN = plan_for_day(SCHEDULE, DAY)[1]
UUID_A = "ed0c23e4-d2c7-428f-bf9c-d6efd38d7f47"
UUID_B = "2051c35c-fef5-4bd9-b8ad-a00c38ff9189"


def pick(slot=1, screen_id=UUID_A, platform="ios", screens=None, **over):
    base = {
        "screens": list(screens or []),
        "slot": slot,
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
    def text(self, plan=PLAN, seen=None):
        return build_digest_messages(DAY, plan, seen)[0]["content"]

    def test_every_slot_becomes_a_numbered_block(self):
        text = self.text()
        for index, (slot, topic) in enumerate(PLAN, start=1):
            self.assertIn(f"Блок {index}", text)
            self.assertIn(topic.query, text)
            self.assertIn(f"slot={index}", text)

    def test_tool_matches_the_kind(self):
        plan = (
            (Slot("s", "i", "c"), Slot("s", "i", "c").rotation()[0]),
            (Slot("f", "w", "b"), Slot("f", "w", "b").rotation()[0]),
            (Slot("w", "", "b"), Slot("w", "", "b").rotation()[0]),
        )
        text = self.text(plan)
        self.assertIn("search_screens", text)
        self.assertIn("search_flows", text)
        self.assertIn("search_sections", text)

    def test_sections_get_no_platform_argument(self):
        slot = Slot("w", "", "b")
        text = self.text(((slot, slot.rotation()[0]),))
        self.assertNotIn('platform="', text.split("Отбери")[0])
        self.assertIn('platform="web"', text)  # но в ответе поле проставить надо

    def test_exclusions_only_for_screens(self):
        without = self.text()
        self.assertNotIn("exclude_screen_ids", without)
        with_ids = self.text(seen={"ios": [UUID_A], "web": [UUID_B]})
        self.assertIn(UUID_A, with_ids)

    def test_flows_never_get_exclusions_they_cannot_use(self):
        slot = Slot("f", "i", "c")
        text = self.text(((slot, slot.rotation()[0]),), seen={"ios": [UUID_A]})
        self.assertNotIn("exclude_screen_ids", text)

    def test_block_count_is_declined_correctly(self):
        slot = Slot("s", "i", "c")
        one = self.text(((slot, slot.rotation()[0]),))
        self.assertIn("1 блок.", one)
        self.assertIn("3 блока.", self.text())


class SearchLimitTest(unittest.TestCase):
    def test_asks_for_a_margin_but_respects_api_ceilings(self):
        self.assertEqual(search_limit("s", 3), 9)
        self.assertEqual(search_limit("s", 1), 8)
        self.assertEqual(search_limit("f", 8), 10)   # у flows потолок 10
        self.assertEqual(search_limit("w", 20), 30)  # у sections 30


class ValidationTest(unittest.TestCase):
    def test_screens_require_a_uuid(self):
        self.assertTrue(is_valid_pick(pick(), "s"))
        self.assertFalse(is_valid_pick(pick(screen_id="not-a-uuid"), "s"))

    def test_flows_and_sections_only_need_a_non_empty_id(self):
        self.assertTrue(is_valid_pick(pick(screen_id="flow-42"), "f"))
        self.assertFalse(is_valid_pick(pick(screen_id="  "), "f"))

    def test_links_must_come_from_mobbin(self):
        self.assertFalse(is_valid_pick(pick(mobbin_url="https://example.com/x"), "s"))
        self.assertFalse(is_valid_pick(pick(image_url="ftp://mobbin.com/x"), "s"))

    def test_platform_must_be_known(self):
        self.assertFalse(is_valid_pick(pick(platform="android"), "s"))


class ParseDigestTest(unittest.TestCase):
    def parse(self, payload, seen=None):
        return parse_digest(json.dumps(payload), DAY, PLAN, seen)

    def test_picks_land_in_their_own_sections(self):
        digest = self.parse(
            {"summary": "Итог", "picks": [pick(1), pick(2, UUID_B, "web")]}
        )
        self.assertEqual(len(digest.sections), 2)
        self.assertEqual(digest.sections[0].slot, PLAN[0][0])
        self.assertEqual(digest.sections[1].slot, PLAN[1][0])
        self.assertEqual(digest.summary, "Итог")

    def test_empty_sections_are_dropped_not_shown_blank(self):
        digest = self.parse({"summary": "", "picks": [pick(2, UUID_B, "web")]})
        self.assertEqual(len(digest.sections), 1)
        self.assertEqual(digest.sections[0].slot, PLAN[1][0])

    def test_unknown_slot_numbers_are_ignored(self):
        digest = self.parse({"summary": "", "picks": [pick(1), pick(99, UUID_B)]})
        self.assertEqual(len(digest.picks), 1)

    def test_duplicates_and_already_seen_are_dropped(self):
        digest = self.parse({"summary": "", "picks": [pick(1), pick(1)]})
        self.assertEqual(len(digest.picks), 1)
        with self.assertRaises(CuratorError):
            self.parse({"summary": "", "picks": [pick(1)]}, seen={UUID_A})

    def test_flow_id_survives_in_a_flow_slot(self):
        """Слот с флоу не должен резать id по формату uuid."""
        flow_slot = next(
            (i for i, (slot, _) in enumerate(PLAN, start=1) if slot.kind == "f"), None
        )
        self.assertIsNotNone(flow_slot, "в профиле по умолчанию должен быть блок с флоу")
        digest = self.parse(
            {"summary": "", "picks": [pick(flow_slot, "flow-77", "ios")]}
        )
        self.assertEqual(digest.picks[0].screen_id, "flow-77")

    def test_screen_picks_exclude_flows(self):
        flow_slot = next(i for i, (slot, _) in enumerate(PLAN, start=1) if slot.kind == "f")
        digest = self.parse(
            {"summary": "", "picks": [pick(1), pick(flow_slot, "flow-77", "ios")]}
        )
        self.assertEqual(len(digest.picks), 2)
        self.assertEqual([p.screen_id for p in digest.screen_picks], [UUID_A])

    def test_garbage_raises(self):
        with self.assertRaises(CuratorError):
            parse_digest("не json", DAY, PLAN)
        with self.assertRaises(CuratorError):
            self.parse({"summary": "", "picks": [{"slot": 1}]})


if __name__ == "__main__":
    unittest.main()


class FlowScreensTest(unittest.TestCase):
    """Шаги флоу приходят списком ссылок — их надо принять и почистить."""

    def flow_slot(self):
        return next(i for i, (slot, _) in enumerate(PLAN, start=1) if slot.kind == "f")

    def parse_flow(self, screens):
        index = self.flow_slot()
        payload = {
            "summary": "",
            "picks": [pick(index, "flow-1", "ios", screens=screens)],
        }
        return parse_digest(json.dumps(payload), DAY, PLAN).picks[0]

    def test_steps_are_kept_in_order(self):
        steps = ["https://a/1", "https://a/2", "https://a/3"]
        self.assertEqual(list(self.parse_flow(steps).screens), steps)

    def test_duplicates_and_junk_are_dropped(self):
        got = self.parse_flow(["https://a/1", "https://a/1", "не ссылка", "", "http://a/2"])
        self.assertEqual(list(got.screens), ["https://a/1"])

    def test_absurdly_long_lists_are_capped(self):
        got = self.parse_flow([f"https://a/{i}" for i in range(100)])
        self.assertEqual(len(got.screens), 24)

    def test_missing_field_is_not_an_error(self):
        index = self.flow_slot()
        item = pick(index, "flow-1", "ios")
        item.pop("screens", None)
        digest = parse_digest(json.dumps({"summary": "", "picks": [item]}), DAY, PLAN)
        self.assertEqual(digest.picks[0].screens, ())

    def test_images_falls_back_to_the_single_preview(self):
        index = self.flow_slot()
        got = parse_digest(
            json.dumps({"summary": "", "picks": [pick(index, "flow-1", "ios", screens=[])]}),
            DAY, PLAN,
        ).picks[0]
        self.assertEqual(got.images, (got.image_url,))

    def test_flow_blocks_are_asked_for_all_steps(self):
        text = build_digest_messages(DAY, PLAN)[0]["content"]
        self.assertIn("`screens`", text)
        self.assertIn('image_format="jpg"', text)


class ScoreTest(unittest.TestCase):
    def parse_score(self, value):
        item = pick(1)
        if value is None:
            item.pop("score", None)
        else:
            item["score"] = value
        return parse_digest(json.dumps({"summary": "", "picks": [item]}), DAY, PLAN).picks[0].score

    def test_score_is_kept(self):
        self.assertEqual(self.parse_score(9), 9)

    def test_score_is_clamped_to_the_scale(self):
        self.assertEqual(self.parse_score(0), 1)
        self.assertEqual(self.parse_score(42), 10)

    def test_junk_becomes_the_middle(self):
        self.assertEqual(self.parse_score("много"), 5)
        self.assertEqual(self.parse_score(None), 5)

    def test_schema_requires_a_score(self):
        from inspobot.prompt import PICK_SCHEMA

        self.assertIn("score", PICK_SCHEMA["required"])
        self.assertEqual(PICK_SCHEMA["properties"]["score"]["maximum"], 10)
