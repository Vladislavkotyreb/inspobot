import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from inspobot import catalog, profile
from inspobot.profile import (
    DEFAULT_SCHEDULE,
    Day,
    ProfileError,
    Schedule,
    Slot,
    plan_for_day,
)

MONDAY = date(2026, 9, 14)


class SlotTest(unittest.TestCase):
    def test_platform_mapping(self):
        self.assertEqual(Slot("s", "i", "c").api_platform, "ios")
        self.assertEqual(Slot("s", "w", "b").api_platform, "web")

    def test_sections_are_always_web_and_need_no_platform(self):
        slot = Slot("w", "", "b")
        self.assertFalse(slot.needs_platform)
        self.assertEqual(slot.api_platform, "web")

    def test_title_skips_platform_for_sections(self):
        self.assertEqual(Slot("s", "i", "c").title(), "Экраны · Мобильные · B2C")
        self.assertEqual(Slot("w", "", "b").title(), "Секции сайта · B2B")

    def test_rotation_defaults_to_the_whole_set(self):
        self.assertEqual(Slot("s", "i", "b").rotation(), catalog.SCREENS_B2B)

    def test_rotation_keeps_only_chosen_topics(self):
        slot = Slot("s", "i", "b", topics=("billing", "team"))
        self.assertEqual([t.slug for t in slot.rotation()], ["team", "billing"])

    def test_unknown_topics_fall_back_to_everything(self):
        """Пустая ротация сломала бы выбор темы делением на ноль."""
        self.assertEqual(Slot("s", "i", "b", topics=("нет-такой",)).rotation(), catalog.SCREENS_B2B)


class DefaultScheduleTest(unittest.TestCase):
    def test_is_valid(self):
        DEFAULT_SCHEDULE.validate()

    def test_saturday_is_a_day_off(self):
        self.assertIsNone(DEFAULT_SCHEDULE.days[5])
        self.assertIsNone(plan_for_day(DEFAULT_SCHEDULE, MONDAY + timedelta(days=5)))

    def test_every_working_day_has_a_title_and_blocks(self):
        for index, day in enumerate(DEFAULT_SCHEDULE.days):
            if day is None:
                continue
            self.assertTrue(day.title.strip(), index)
            self.assertTrue(day.slots, index)

    def test_monday_is_about_gamification(self):
        planned = plan_for_day(DEFAULT_SCHEDULE, MONDAY)
        self.assertIsNotNone(planned)
        day, plan = planned
        self.assertEqual(day.title, "Геймификация")
        slugs = {slot.topics for slot, _ in plan}
        self.assertTrue(any("streak" in group for group in slugs))


class ValidationTest(unittest.TestCase):
    def one(self, day):
        return Schedule(days=(day, None, None, None, None, None, None))

    def test_rejects_broken_schedules(self):
        good = Slot("s", "i", "b")
        cases = {
            "не семь дней": Schedule(days=(Day("x", (good,)),)),
            "все пустые": Schedule(days=(None,) * 7),
            "день без блоков": self.one(Day("x", ())),
            "вид": self.one(Day("x", (Slot("z", "i", "b"),))),
            "аудитория": self.one(Day("x", (Slot("s", "i", "z"),))),
            "нет платформы": self.one(Day("x", (Slot("s", "", "b"),))),
            "платформа у секций": self.one(Day("x", (Slot("w", "i", "b"),))),
            "количество": self.one(Day("x", (Slot("s", "i", "b", count=0),))),
            "чужая тема": self.one(Day("x", (Slot("s", "i", "b", topics=("paywall",)),))),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                with self.assertRaises(ProfileError):
                    bad.validate()

    def test_error_names_the_weekday(self):
        bad = Schedule(days=(None, Day("x", (Slot("z", "i", "b"),)), None, None, None, None, None))
        with self.assertRaises(ProfileError) as caught:
            bad.validate()
        self.assertIn("Вторник", str(caught.exception))


class RotationTest(unittest.TestCase):
    def slugs(self, schedule, weeks):
        out = []
        for week in range(weeks):
            planned = plan_for_day(schedule, MONDAY + timedelta(weeks=week))
            out.append(planned[1][0][1].slug)
        return out

    def test_topic_changes_every_week(self):
        schedule = DEFAULT_SCHEDULE
        got = self.slugs(schedule, 4)
        self.assertEqual(len(set(got)), 4)

    def test_full_circle_then_repeat(self):
        slot = DEFAULT_SCHEDULE.days[0].slots[0]
        size = len(slot.rotation())
        got = self.slugs(DEFAULT_SCHEDULE, size + 1)
        self.assertEqual(len(set(got[:size])), size)
        self.assertEqual(got[size], got[0])

    def test_seven_topics_do_not_get_stuck(self):
        """Счёт по дням застрял бы: один и тот же день недели — раз в 7 суток."""
        seven = tuple(t.slug for t in catalog.SCREENS_B2C[:7])
        schedule = Schedule(
            days=(Day("x", (Slot("s", "i", "c", topics=seven),)),) + (None,) * 6
        )
        got = self.slugs(schedule, 7)
        self.assertEqual(len(set(got)), 7)

    def test_blocks_of_one_day_do_not_move_in_lockstep(self):
        first, second = [], []
        for week in range(5):
            _, plan = plan_for_day(DEFAULT_SCHEDULE, MONDAY + timedelta(weeks=week))
            first.append(plan[0][1].slug)
            second.append(plan[1][1].slug)
        self.assertNotEqual(first, second)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "nested" / "profile.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_gives_the_default(self):
        self.assertEqual(profile.load(self.path), DEFAULT_SCHEDULE)

    def test_roundtrip_keeps_days_off(self):
        profile.save(self.path, DEFAULT_SCHEDULE)
        self.assertEqual(profile.load(self.path), DEFAULT_SCHEDULE)

    def test_old_single_profile_format_still_loads(self):
        """Файл, записанный прежней версией, не должен ронять утренний запуск."""
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"slots": [{"kind": "s", "platform": "i", "audience": "c"}]}),
            encoding="utf-8",
        )
        schedule = profile.load(self.path)
        self.assertTrue(all(day is not None for day in schedule.days))

    def test_broken_file_is_reported_not_swallowed(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{не json", encoding="utf-8")
        with self.assertRaises(ProfileError):
            profile.load(self.path)

    def test_invalid_content_is_rejected_on_load(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"days": {"mon": {"title": "x", "slots": [{"kind": "s", "audience": "b"}]}}}),
            encoding="utf-8",
        )
        with self.assertRaises(ProfileError):
            profile.load(self.path)

    def test_saving_an_invalid_schedule_is_refused(self):
        with self.assertRaises(ProfileError):
            profile.save(self.path, Schedule(days=(None,) * 7))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
