import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from inspobot import catalog, profile
from inspobot.profile import DEFAULT_PROFILE, Profile, ProfileError, Slot

DAY = date(2026, 9, 10)


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
        """Пустой список тем сломал бы ротацию делением на ноль."""
        slot = Slot("s", "i", "b", topics=("нет-такой",))
        self.assertEqual(slot.rotation(), catalog.SCREENS_B2B)


class ValidationTest(unittest.TestCase):
    def test_default_profile_is_valid(self):
        DEFAULT_PROFILE.validate()

    def test_rejects_broken_slots(self):
        cases = {
            "нет слотов": Profile(()),
            "вид": Profile((Slot("z", "i", "b"),)),
            "аудитория": Profile((Slot("s", "i", "z"),)),
            "нет платформы": Profile((Slot("s", "", "b"),)),
            "платформа у секций": Profile((Slot("w", "i", "b"),)),
            "количество": Profile((Slot("s", "i", "b", count=0),)),
            "чужая тема": Profile((Slot("s", "i", "b", topics=("paywall",)),)),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                with self.assertRaises(ProfileError):
                    bad.validate()


class PlanTest(unittest.TestCase):
    def test_one_topic_per_slot(self):
        plan = profile.plan_for_day(DEFAULT_PROFILE, DAY)
        self.assertEqual(len(plan), len(DEFAULT_PROFILE.slots))
        for slot, topic in plan:
            self.assertIn(topic, slot.rotation())

    def test_topic_changes_every_day_and_returns_after_a_full_circle(self):
        slot = DEFAULT_PROFILE.slots[0]
        size = len(slot.rotation())
        seen = [
            profile.plan_for_day(DEFAULT_PROFILE, DAY + timedelta(days=i))[0][1].slug
            for i in range(size)
        ]
        self.assertEqual(len(set(seen)), size)
        after = profile.plan_for_day(DEFAULT_PROFILE, DAY + timedelta(days=size))[0][1].slug
        self.assertEqual(after, seen[0])

    def test_slots_do_not_move_in_lockstep(self):
        first, second = [], []
        for i in range(6):
            plan = profile.plan_for_day(DEFAULT_PROFILE, DAY + timedelta(days=i))
            first.append(plan[0][1].slug)
            second.append(plan[1][1].slug)
        self.assertNotEqual(first, second)

    def test_single_topic_slot_repeats_that_topic(self):
        one = Profile((Slot("s", "i", "b", topics=("billing",)),))
        for i in range(3):
            plan = profile.plan_for_day(one, DAY + timedelta(days=i))
            self.assertEqual(plan[0][1].slug, "billing")


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "nested" / "profile.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_gives_the_default(self):
        self.assertEqual(profile.load(self.path), DEFAULT_PROFILE)

    def test_roundtrip(self):
        original = Profile(
            (Slot("f", "w", "b", count=2, topics=("invite",)), Slot("w", "", "c", count=4))
        )
        profile.save(self.path, original)
        self.assertEqual(profile.load(self.path), original)

    def test_saved_file_is_readable_json(self):
        profile.save(self.path, DEFAULT_PROFILE)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["slots"]), len(DEFAULT_PROFILE.slots))

    def test_broken_file_is_reported_not_swallowed(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{не json", encoding="utf-8")
        with self.assertRaises(ProfileError):
            profile.load(self.path)

    def test_invalid_content_is_rejected_on_load(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"slots": [{"kind": "s", "audience": "b"}]}), encoding="utf-8")
        with self.assertRaises(ProfileError):
            profile.load(self.path)

    def test_saving_an_invalid_profile_is_refused(self):
        with self.assertRaises(ProfileError):
            profile.save(self.path, Profile(()))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
