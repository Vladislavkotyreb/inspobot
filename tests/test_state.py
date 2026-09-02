import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from inspobot.state import SeenScreen, Store

DAY = date(2026, 9, 2)


def screen(i, platform="ios"):
    sid = f"{i:08d}-0000-4000-8000-000000000000"
    return SeenScreen(sid, platform, f"App {i}", f"https://mobbin.com/screens/{sid}")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "nested" / "state.sqlite3")

    def tearDown(self):
        self.dir.cleanup()

    def test_creates_parent_directory(self):
        self.assertTrue(self.store.path.exists())

    def test_run_is_idempotent_per_day(self):
        self.assertFalse(self.store.sent_today(DAY))
        run = self.store.start_run(DAY)
        self.assertFalse(self.store.sent_today(DAY))  # начатый, но не завершённый
        self.store.finish_run(run, ok=True, picks=10)
        self.assertTrue(self.store.sent_today(DAY))
        self.assertFalse(self.store.sent_today(DAY + timedelta(days=1)))

    def test_failed_run_does_not_block_retry(self):
        run = self.store.start_run(DAY)
        self.store.finish_run(run, ok=False, error="boom")
        self.assertFalse(self.store.sent_today(DAY))

    def test_seen_is_per_platform_and_deduplicated(self):
        self.store.mark_seen([screen(1), screen(2), screen(3, "web")], DAY)
        self.store.mark_seen([screen(1)], DAY)  # повтор не должен ломать вставку
        self.assertEqual(len(self.store.recent_seen_ids("ios")), 2)
        self.assertEqual(len(self.store.recent_seen_ids("web")), 1)

    def test_recent_ids_capped_at_mobbin_limit(self):
        self.store.mark_seen([screen(i) for i in range(150)], DAY)
        self.assertEqual(len(self.store.recent_seen_ids("ios", limit=1000)), 100)

    def test_known_ids(self):
        self.store.mark_seen([screen(7)], DAY)
        known = self.store.known_ids([screen(7).screen_id, screen(8).screen_id])
        self.assertEqual(known, {screen(7).screen_id})
        self.assertEqual(self.store.known_ids([]), set())


if __name__ == "__main__":
    unittest.main()
