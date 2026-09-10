import tempfile
import unittest
from pathlib import Path

from inspobot import chats


class LoadChatsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "chats.txt"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_missing_file_falls_back_to_the_single_chat(self):
        self.assertEqual(chats.load(self.path, "42"), ("42",))
        self.assertEqual(chats.load(self.path), ())

    def test_one_id_per_line(self):
        self.write("42\n-100500\n777\n")
        self.assertEqual(chats.load(self.path), ("42", "-100500", "777"))

    def test_comments_and_blanks_are_ignored(self):
        self.write("# подписчики\n\n42  # Влад\n\n  -100500\n#77\n")
        self.assertEqual(chats.load(self.path), ("42", "-100500"))

    def test_duplicates_are_collapsed_keeping_order(self):
        self.write("42\n7\n42\n")
        self.assertEqual(chats.load(self.path), ("42", "7"))

    def test_empty_file_falls_back(self):
        self.write("# только комментарии\n\n")
        self.assertEqual(chats.load(self.path, "42"), ("42",))


if __name__ == "__main__":
    unittest.main()
