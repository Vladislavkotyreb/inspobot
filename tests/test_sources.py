"""Объявление источников: разбор файлов, правки, проверки."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inspobot.sources import (
    SECTIONS,
    SOURCES,
    Source,
    SourcesError,
    apply_overrides,
    by_section,
    default_studios,
    enabled,
    from_dict,
    load,
    parse_studios,
    studio_key,
)


def a_source(**changes) -> Source:
    base = dict(
        key="x", section="cssda", title="X", url="https://a.ru/list",
        ways=("html",), link_re=r"a\.ru/case/",
    )
    base.update(changes)
    return Source(**base)


class BuiltinTests(unittest.TestCase):
    def test_every_builtin_source_passes_its_own_check(self):
        for source in SOURCES:
            with self.subTest(source.key):
                source.check()

    def test_every_section_has_at_least_one_source(self):
        """Раздел без источника — заголовок, который никогда не наполнится."""
        keys = {s.section for s in SOURCES} | {s.section for s in default_studios()}
        for spec in SECTIONS:
            self.assertIn(spec.key, keys, spec.key)

    def test_section_keys_are_unique(self):
        keys = [s.key for s in SECTIONS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_source_keys_are_unique(self):
        keys = [s.key for s in SOURCES] + [s.key for s in default_studios()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_short_names_fit_a_caption(self):
        for spec in SECTIONS:
            self.assertLessEqual(len(spec.name()), 24, spec.key)


class CheckTests(unittest.TestCase):
    def test_unknown_way(self):
        with self.assertRaisesRegex(SourcesError, "неизвестный способ"):
            a_source(ways=("grpc",)).check()

    def test_no_ways(self):
        with self.assertRaisesRegex(SourcesError, "ни одного способа"):
            a_source(ways=()).check()

    def test_unknown_section(self):
        with self.assertRaisesRegex(SourcesError, "раздела"):
            a_source(section="нетакого").check()

    def test_html_without_link_re_is_refused(self):
        """Без правила разбор выгребет всю навигацию — это не находки."""
        with self.assertRaisesRegex(SourcesError, "выгребет всю навигацию"):
            a_source(link_re="").check()

    def test_json_without_fields_is_refused(self):
        with self.assertRaisesRegex(SourcesError, "без карты полей"):
            a_source(ways=("json",), fields={}).check()

    def test_broken_regex_is_named(self):
        with self.assertRaisesRegex(SourcesError, "link_re не компилируется"):
            a_source(link_re="([").check()


class SourceFieldTests(unittest.TestCase):
    def test_way_url_falls_back_to_url(self):
        source = a_source(way_urls={"rss": "https://a.ru/feed"}, ways=("rss", "html"))
        self.assertEqual(source.way_url("rss"), "https://a.ru/feed")
        self.assertEqual(source.way_url("html"), "https://a.ru/list")

    def test_base_is_scheme_and_host(self):
        self.assertEqual(a_source().base(), "https://a.ru")


class StudioTests(unittest.TestCase):
    def test_key_from_host(self):
        self.assertEqual(studio_key("https://www.Red-Collar.ru/works/"), "studio:red-collar-ru")

    def test_parses_lines_and_skips_comments(self):
        text = (
            "# список пополняется\n"
            "\n"
            "Red Collar | https://redcollar.ru/works/ | redcollar\\.ru/works/\n"
            "AIC | https://aic.ru/works | aic\\.ru/works/ | 2\n"
        )
        got = parse_studios(text)
        self.assertEqual([s.title for s in got], ["Red Collar", "AIC"])
        self.assertEqual(got[1].limit, 2)
        self.assertEqual(got[0].section, "studios")

    def test_bad_line_names_its_number(self):
        with self.assertRaisesRegex(SourcesError, "строка 2"):
            parse_studios("Ok | https://a.ru/ | a\\.ru\nсломано\n")

    def test_studios_take_one_case_each_by_default(self):
        for source in default_studios():
            self.assertEqual(source.limit, 1, source.key)


class OverrideTests(unittest.TestCase):
    def test_patch_existing(self):
        got = apply_overrides(
            [a_source(key="dprofile", section="dprofile")],
            {"sources": {"dprofile": {"url": "https://dprofile.ru/cases", "limit": 5}}},
        )
        self.assertEqual(got[0].url, "https://dprofile.ru/cases")
        self.assertEqual(got[0].limit, 5)

    def test_patch_keeps_order_of_builtins(self):
        base = [a_source(key="a"), a_source(key="b"), a_source(key="c")]
        got = apply_overrides(base, {"sources": {"b": {"limit": 9}}})
        self.assertEqual([s.key for s in got], ["a", "b", "c"])

    def test_patch_for_unknown_source_lists_what_exists(self):
        with self.assertRaisesRegex(SourcesError, "неизвестного источника.*Есть: x"):
            apply_overrides([a_source()], {"sources": {"нетакого": {"limit": 1}}})

    def test_unknown_field_is_named(self):
        with self.assertRaisesRegex(SourcesError, "неизвестные поля"):
            apply_overrides([a_source()], {"sources": {"x": {"лимит": 1}}})

    def test_unknown_top_level_key(self):
        with self.assertRaisesRegex(SourcesError, "лишние ключи"):
            apply_overrides([a_source()], {"источники": {}})

    def test_add_new_source(self):
        got = apply_overrides(
            [a_source()],
            {"add": [{"key": "new", "section": "tilda", "title": "Н",
                      "url": "https://n.ru/", "link_re": "n\\.ru/x"}]},
        )
        self.assertEqual([s.key for s in got], ["x", "new"])

    def test_add_without_required_fields(self):
        with self.assertRaisesRegex(SourcesError, "не хватает полей"):
            from_dict({"key": "a", "section": "tilda"})

    def test_ways_from_json_become_a_tuple(self):
        got = from_dict({"key": "a", "section": "tilda", "title": "A",
                         "url": "https://a.ru/", "ways": ["rss", "html"],
                         "link_re": "a\\.ru/x"})
        self.assertEqual(got.ways, ("rss", "html"))

    def test_studios_shorthand(self):
        got = apply_overrides(
            [],
            {"studios": [{"title": "Новая", "url": "https://new.ru/w/", "link_re": "new\\.ru/w/"}]},
        )
        self.assertEqual(got[0].key, "studio:new-ru")
        self.assertEqual(got[0].section, "studios")


class LoadTests(unittest.TestCase):
    def test_load_without_files_uses_builtins(self):
        got = load(Path("/несуществующий/sources.json"), Path("/несуществующий/studios.txt"))
        self.assertEqual(len(got), len(SOURCES) + len(default_studios()))

    def test_load_applies_overrides_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text(
                json.dumps({"sources": {"snapensnap": {"enabled": False}}}), encoding="utf-8"
            )
            got = load(path, Path(tmp) / "нет.txt")
            self.assertFalse(next(s for s in got if s.key == "snapensnap").enabled)
            self.assertNotIn("snapensnap", [s.key for s in enabled(got)])

    def test_broken_json_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text("{не json", encoding="utf-8")
            with self.assertRaisesRegex(SourcesError, "не читается как JSON"):
                load(path, Path(tmp) / "нет.txt")

    def test_override_that_breaks_a_source_is_caught_at_load(self):
        """Правка, оставившая html без правила, обязана упасть при загрузке,
        а не в шесть утра пустым разделом."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text(json.dumps({"sources": {"cssda": {"link_re": ""}}}), encoding="utf-8")
            with self.assertRaises(SourcesError):
                load(path, Path(tmp) / "нет.txt")

    def test_studios_file_replaces_builtin_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            studios = Path(tmp) / "studios.txt"
            studios.write_text("Одна | https://one.ru/w/ | one\\.ru/w/\n", encoding="utf-8")
            got = load(Path(tmp) / "нет.json", studios)
            keys = [s.key for s in got if s.section == "studios"]
            self.assertEqual(keys, ["studio:one-ru"])


class BySectionTests(unittest.TestCase):
    def test_follows_section_order_and_skips_empty(self):
        got = by_section([a_source(key="t", section="tilda"), a_source(key="c", section="cssda")])
        self.assertEqual([spec.key for spec, _ in got], ["cssda", "tilda"])

    def test_disabled_sources_do_not_create_a_section(self):
        self.assertEqual(by_section([a_source(enabled=False)]), [])

    def test_groups_keep_declaration_order(self):
        got = by_section([
            a_source(key="a", section="dribbble"),
            a_source(key="b", section="dribbble"),
        ])
        self.assertEqual([s.key for s in got[0][1]], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
