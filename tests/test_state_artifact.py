"""Чистые части восстановления из артефакта: выбор свежайшего и распаковка."""

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import state_artifact  # noqa: E402


def zipped(name: str, content: bytes = b"sqlite") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(name, content)
    return buffer.getvalue()


class PickLatestTest(unittest.TestCase):
    def test_newest_alive_wins(self):
        artifacts = [
            {"id": 1, "created_at": "2026-09-01T08:00:00Z", "expired": False},
            {"id": 3, "created_at": "2026-09-11T08:00:00Z", "expired": False},
            {"id": 2, "created_at": "2026-09-05T08:00:00Z", "expired": False},
        ]
        self.assertEqual(state_artifact.pick_latest(artifacts)["id"], 3)

    def test_expired_are_skipped_even_if_newer(self):
        """Просроченный артефакт GitHub ещё показывает, но скачать его нельзя."""
        artifacts = [
            {"id": 1, "created_at": "2026-09-01T08:00:00Z", "expired": False},
            {"id": 2, "created_at": "2026-09-11T08:00:00Z", "expired": True},
        ]
        self.assertEqual(state_artifact.pick_latest(artifacts)["id"], 1)

    def test_nothing_alive(self):
        self.assertIsNone(state_artifact.pick_latest([]))
        self.assertIsNone(state_artifact.pick_latest([{"id": 1, "expired": True}]))


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.target = Path(self.dir.name) / "var" / "inspobot.sqlite3"

    def tearDown(self):
        self.dir.cleanup()

    def test_extracts_by_file_name_regardless_of_folder(self):
        self.assertTrue(state_artifact.extract(zipped("inspobot.sqlite3", b"abc"), self.target))
        self.assertEqual(self.target.read_bytes(), b"abc")

    def test_reports_missing_file(self):
        self.assertFalse(state_artifact.extract(zipped("other.bin"), self.target))
        self.assertFalse(self.target.exists())

    def test_creates_parent_directory(self):
        state_artifact.extract(zipped("inspobot.sqlite3"), self.target)
        self.assertTrue(self.target.parent.is_dir())


class RestoreExitCodesTest(unittest.TestCase):
    """Главное обещание скрипта: сбой скачивания — это остановка, а не
    пустая база, которая потом затрёт историю."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.target = Path(self.dir.name) / "inspobot.sqlite3"
        self.env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}

    def tearDown(self):
        self.dir.cleanup()

    def run_restore(self, artifacts, download=None):
        from unittest import mock

        with mock.patch.dict("os.environ", self.env), \
             mock.patch.object(state_artifact, "list_artifacts", return_value=artifacts), \
             mock.patch.object(state_artifact, "download", side_effect=download) if download else mock.patch.object(state_artifact, "download", return_value=zipped("inspobot.sqlite3", b"db")):
            return state_artifact.restore(self.target)

    def test_no_artifact_is_a_fresh_start(self):
        self.assertEqual(self.run_restore([]), 0)
        self.assertFalse(self.target.exists())

    def test_restores_when_available(self):
        artifacts = [{"id": 1, "created_at": "2026-09-11T08:00:00Z", "expired": False,
                      "archive_download_url": "https://x"}]
        self.assertEqual(self.run_restore(artifacts), 0)
        self.assertEqual(self.target.read_bytes(), b"db")

    def test_download_failure_stops_the_run(self):
        import urllib.error

        artifacts = [{"id": 1, "created_at": "2026-09-11T08:00:00Z", "expired": False,
                      "archive_download_url": "https://x"}]
        code = self.run_restore(artifacts, download=urllib.error.URLError("сеть"))
        self.assertEqual(code, 1)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
