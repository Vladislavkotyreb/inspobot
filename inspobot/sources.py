"""Откуда берётся лента: семь разделов дайджеста, объявленные данными.

Источник здесь — не код, а запись: адрес, способ разбора и карта полей.
Сайт перевёрстан — правится одна строка. На сервере правится вообще без
выката, в `var/sources.json`: тот же набор полей, слитый поверх этого.

Способы разбора выстроены лесенкой. Источник перечисляет их в `ways`,
`gather` идёт по списку и останавливается на первом, который вернул хоть
что-то; `--probe` показывает, какой именно сработал и что пришло.

Почему у большинства источников способ — `html`, а не аккуратный разбор
вёрстки. Семь сайтов — это семь парсеров, которые сломаются семью разными
способами, и каждый раз молча. Одна регулярка на ссылку плюс og-теги самой
находки дают ту же карточку (заголовок, автор, картинка), а когда всё-таки
ломаются — ломаются одинаково и видно это одной командой.

Ни одного обращения к Claude здесь нет и не подразумевается: лента состоит
из того, что источники и так публикуют машиночитаемо. Прогон стоит ноль.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent

# Способы разбора, которые понимает `gather`. Значение вне списка — опечатка
# в переопределениях, и лучше сказать об этом при загрузке, чем молча
# пропустить источник в шесть утра.
# «mobbin» — не HTTP-страница, а вызов инструмента MCP; его выполняет
# отдельный сборщик, переданный в `gather` (см. mobbin_feed.puller).
WAYS = ("rss", "json", "html", "mobbin")


class SourcesError(RuntimeError):
    pass


@dataclass(frozen=True)
class SectionSpec:
    """Раздел дайджеста. Источников в разделе может быть сколько угодно:
    у Dribbble их три (по категории), у студий — сколько наберётся."""

    key: str
    title: str
    icon: str
    limit: int = 3
    # Короткое имя для подписи под картинкой. Полное («Dribbble — Product,
    # Web, Mobile») перечисляет весь раздел и в карточке одного шота читается
    # как ошибка: шот-то из одной категории.
    short: str = ""

    def name(self) -> str:
        return self.short or self.title


# Порядок разделов в письме. Первым идёт то, что смотрят ради одной картинки,
# дальше — то, ради чего открывают кейс целиком.
SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec("mobbin", "Mobbin — экраны недели", "📱", limit=3, short="Mobbin"),
    SectionSpec("cssda", "Сайты дня CSS Design Awards", "🏆", limit=3, short="CSS Design Awards"),
    SectionSpec("studios", "Кейсы дизайн-студий и агентств", "🏛", limit=4, short="Студии"),
    SectionSpec("behance", "Behance — лента UX/UI", "🎨", limit=4, short="Behance"),
    SectionSpec("dribbble", "Dribbble — Product, Web, Mobile", "🏀", limit=6, short="Dribbble"),
    SectionSpec("dprofile", "DProfile — «Интерфейсы»", "🇷🇺", limit=3, short="DProfile"),
    SectionSpec("tilda", "Награды Made on Tilda", "🧱", limit=3, short="Made on Tilda"),
    SectionSpec("snapensnap", "Новые приложения Snapensnap", "📲", limit=3, short="Snapensnap"),
)

SECTION_BY_KEY = {spec.key: spec for spec in SECTIONS}


@dataclass(frozen=True)
class Source:
    key: str
    section: str
    title: str
    url: str
    ways: tuple[str, ...] = ("html",)
    limit: int = 3
    enabled: bool = True
    # Адрес отдельно под способ: лента живёт не там же, где страница-список.
    way_urls: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    # html: какие ссылки со страницы считать находками, а какие — навигацией.
    link_re: str = ""
    deny_re: str = ""
    # json: где лежит массив, как называются поля, из чего собрать адрес.
    items_path: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)
    url_template: str = ""
    # Дочитать og-теги страницы находки: ещё один запрос на каждую, зато
    # заголовок и картинка настоящие, а не текст ссылки в списке.
    enrich: bool = True
    # Картинки нет даже в og — заказать скриншот страницы. Осмысленно для
    # наградных списков: там находка и есть сайт целиком.
    shot: bool = False
    # Подпись источника в карточке брать из самой находки, а не из `title`.
    # Нужно там, где источник один, а рубрика меняется: у Mobbin это тема
    # недели, и писать в карточке неизменное «Экраны» было бы бессмысленно.
    origin_from_tag: bool = False
    note: str = ""

    def way_url(self, way: str) -> str:
        return self.way_urls.get(way) or self.url

    def base(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""

    def check(self) -> None:
        bad = [w for w in self.ways if w not in WAYS]
        if bad:
            raise SourcesError(
                f"{self.key}: неизвестный способ разбора {bad} — можно только {list(WAYS)}"
            )
        if not self.ways:
            raise SourcesError(f"{self.key}: не указано ни одного способа разбора")
        if self.section not in SECTION_BY_KEY:
            raise SourcesError(f"{self.key}: раздела «{self.section}» нет в SECTIONS")
        for name in ("link_re", "deny_re"):
            pattern = getattr(self, name)
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise SourcesError(f"{self.key}: {name} не компилируется — {exc}") from exc
        if self.ways == ("mobbin",) and self.enrich:
            raise SourcesError(
                f"{self.key}: страницу Mobbin дочитывать нечем — она за входом. "
                "Поставьте enrich=False"
            )
        if "html" in self.ways and not self.link_re:
            raise SourcesError(
                f"{self.key}: способ html без link_re выгребет всю навигацию сайта"
            )
        if "json" in self.ways and not self.fields:
            raise SourcesError(f"{self.key}: способ json без карты полей ничего не соберёт")


# Заголовок как у обычного браузера. Без него половина каталогов отдаёт
# заглушку Cloudflare, а не страницу, и разбор молча возвращает пустоту.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}


SOURCES: tuple[Source, ...] = (
    # Собственный источник бота. Идёт не по HTTP, а вызовом инструмента MCP
    # напрямую — токен выписан на сам сервер Mobbin, Claude в этой цепочке
    # не участвует. Заголовка «самое популярное» здесь нет намеренно: у
    # поиска Mobbin нет ни сортировки по популярности, ни окна «за неделю»
    # (см. mobbin_feed).
    Source(
        key="mobbin",
        section="mobbin",
        title="Тема недели",
        url="mcp://mobbin/search_screens",
        ways=("mobbin",),
        limit=3,
        enrich=False,
        origin_from_tag=True,
        note="тема меняется каждую неделю; отбор — ранжирование Mobbin",
    ),
    # Адрес находки подтверждён: /sites/<слаг>/<номер>/.
    Source(
        key="cssda",
        section="cssda",
        title="Site of the Day",
        url="https://www.cssdesignawards.com/wotd-award-winners",
        ways=("html",),
        link_re=r"cssdesignawards\.com/sites/[^/\"'?#]+/\d+",
        limit=3,
        shot=True,
        note="победители «сайт дня»",
    ),
    # У Behance есть и лента, и внутренняя ручка поиска. Ленту пробуем первой:
    # она не требует заголовков и не ломается от смены имён полей.
    Source(
        key="behance",
        section="behance",
        title="UX/UI, больше всего лайков",
        url="https://www.behance.net/search/projects?field=ui%2Fux&sort=appreciations&time=week",
        ways=("rss", "html"),
        way_urls={"rss": "https://www.behance.net/feeds/projects"},
        headers=_XHR,
        link_re=r"behance\.net/gallery/\d+/",
        limit=4,
        note="лента UX/UI, отсортированная по лайкам за неделю",
    ),
    Source(
        key="dribbble:product",
        section="dribbble",
        title="Product",
        url="https://dribbble.com/shots/popular/product-design",
        ways=("html",),
        link_re=r"dribbble\.com/shots/\d+",
        deny_re=r"/(attachments|comments)\b",
        limit=2,
    ),
    Source(
        key="dribbble:web",
        section="dribbble",
        title="Web",
        url="https://dribbble.com/shots/popular/web-design",
        ways=("html",),
        link_re=r"dribbble\.com/shots/\d+",
        deny_re=r"/(attachments|comments)\b",
        limit=2,
    ),
    Source(
        key="dribbble:mobile",
        section="dribbble",
        title="Mobile",
        url="https://dribbble.com/shots/popular/mobile",
        ways=("html",),
        link_re=r"dribbble\.com/shots/\d+",
        deny_re=r"/(attachments|comments)\b",
        limit=2,
    ),
    # Адрес кейса подтверждён: /case/<номер>/<слаг>. «Выбор жюри» — это и есть
    # список популярного, отобранного руками.
    Source(
        key="dprofile",
        section="dprofile",
        title="Выбор жюри",
        url="https://dprofile.ru/cases",
        ways=("html",),
        link_re=r"dprofile\.ru/case/\d+",
        limit=3,
        note="кейсы с пометкой «Выбор жюри»",
    ),
    Source(
        key="tilda",
        section="tilda",
        title="Made on Tilda",
        url="https://tilda.cc/made-on-tilda/",
        ways=("html",),
        # Награда достаётся внешнему сайту, поэтому ссылки ведут наружу, а не
        # внутрь tilda.cc. Отсюда правило «что угодно, кроме самой Тильды».
        link_re=r"^https?://(?!(?:[a-z0-9-]+\.)*tilda\.(?:cc|ws)/)[^/]+\.[a-z]{2,}",
        deny_re=r"(facebook|twitter|x\.com|instagram|vk\.com|t\.me|youtube|linkedin|pinterest)\.",
        limit=3,
        shot=True,
        note="сайты, взявшие награду",
    ),
    # Адрес не подтверждён: поиском сайт не находится, а достучаться отсюда
    # нельзя — egress-политика песочницы рубит соединение. Раздел останется
    # пустым, пока `--probe` на сервере не покажет настоящий адрес; пустой
    # источник дайджест просто пропускает.
    Source(
        key="snapensnap",
        section="snapensnap",
        title="Новые приложения",
        url="https://snapensnap.com/",
        ways=("rss", "html"),
        way_urls={"rss": "https://snapensnap.com/feed"},
        link_re=r"snapensnap\.com/(app|apps|s)/[^\"'?#]+",
        limit=3,
        note="адрес не подтверждён — проверить командой probe",
    ),
)


# Стартовый список студий. Он и задуман пополняемым: строки лежат в
# config/studios.txt (или var/studios.txt), формат — «Название | адрес |
# регулярка ссылки кейса», и добавить студию можно, не трогая Python.
STUDIOS_FILE = "studios.txt"

STUDIOS: tuple[tuple[str, str, str], ...] = (
    ("Студия Артемия Лебедева", "https://www.artlebedev.ru/everything/", r"artlebedev\.ru/[^/\"'?#]+/[^/\"'?#]+/$"),
    ("AIC", "https://aic.ru/works", r"aic\.ru/works/[^\"'?#]+"),
    ("Red Collar", "https://redcollar.ru/works/", r"redcollar\.ru/works/[^\"'?#]+"),
    ("Ony", "https://ony.ru/projects/", r"ony\.ru/projects/[^\"'?#]+"),
    ("Agima", "https://www.agima.ru/cases/", r"agima\.ru/cases/[^\"'?#]+"),
    ("Work & Co", "https://work.co/work/", r"work\.co/work/[^\"'?#]+"),
    ("MetaLab", "https://www.metalab.com/work", r"metalab\.com/work/[^\"'?#]+"),
    ("Locomotive", "https://locomotive.ca/en/work", r"locomotive\.ca/[a-z]{2}/work/[^\"'?#]+"),
    ("Active Theory", "https://activetheory.net/work", r"activetheory\.net/work/[^\"'?#]+"),
    ("Basic", "https://basicagency.com/work", r"basicagency\.com/work/[^\"'?#]+"),
)


def studio_key(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return "studio:" + re.sub(r"[^a-z0-9]+", "-", host).strip("-")


def studio_source(title: str, url: str, link_re: str, limit: int = 1) -> Source:
    """Студия — обычный источник со своим разделом. Берём по одному кейсу:
    иначе одна студия с длинной страницей вытеснит из раздела все остальные."""
    return Source(
        key=studio_key(url),
        section="studios",
        title=title,
        url=url,
        ways=("html",),
        link_re=link_re,
        limit=limit,
        shot=True,
    )


def parse_studios(text: str) -> tuple[Source, ...]:
    """Разбор `studios.txt`. Строка — «Название | адрес | регулярка»,
    пустые строки и решётки пропускаются.

    Кривая строка не роняет весь файл, а называет себя по номеру: список
    пополняется руками, и опечатка в одной студии не должна отменять письмо.
    """
    found: list[Source] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or not all(parts[:3]):
            raise SourcesError(
                f"{STUDIOS_FILE}, строка {number}: нужно «Название | адрес | регулярка», "
                f"а пришло {line!r}"
            )
        title, url, link_re = parts[0], parts[1], parts[2]
        limit = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        found.append(studio_source(title, url, link_re, limit))
    return tuple(found)


def default_studios() -> tuple[Source, ...]:
    return tuple(studio_source(*row) for row in STUDIOS)


def _apply(source: Source, patch: Mapping[str, Any]) -> Source:
    known = {f.name for f in source.__dataclass_fields__.values()}
    unknown = sorted(set(patch) - known)
    if unknown:
        raise SourcesError(f"{source.key}: неизвестные поля {unknown}")
    changes = dict(patch)
    if "ways" in changes:
        changes["ways"] = tuple(changes["ways"])
    return replace(source, **changes)


def from_dict(row: Mapping[str, Any]) -> Source:
    missing = [n for n in ("key", "section", "title", "url") if not row.get(n)]
    if missing:
        raise SourcesError(f"В описании источника не хватает полей: {missing}")
    known = {f.name for f in Source.__dataclass_fields__.values()}
    unknown = sorted(set(row) - known)
    if unknown:
        raise SourcesError(f"{row['key']}: неизвестные поля {unknown}")
    data = dict(row)
    if "ways" in data:
        data["ways"] = tuple(data["ways"])
    return Source(**data)


def apply_overrides(sources: Sequence[Source], data: Mapping[str, Any]) -> tuple[Source, ...]:
    """Слить `var/sources.json` поверх встроенного списка.

    Три ключа: `sources` — правки по ключу источника, `add` — новые
    источники целиком, `studios` — короткая запись для студий. Всё
    необязательно, и порядок встроенных источников сохраняется: раздел
    читается сверху вниз, и переставлять его правкой файла не надо.
    """
    unknown = sorted(set(data) - {"sources", "add", "studios"})
    if unknown:
        raise SourcesError(f"В переопределениях лишние ключи: {unknown}")

    by_key = {s.key: s for s in sources}
    order = [s.key for s in sources]

    for key, patch in (data.get("sources") or {}).items():
        if key not in by_key:
            raise SourcesError(
                f"Правка для неизвестного источника «{key}». Есть: {', '.join(order)}"
            )
        by_key[key] = _apply(by_key[key], patch)

    for row in data.get("studios") or []:
        source = studio_source(
            row["title"], row["url"], row["link_re"], int(row.get("limit", 1))
        )
        if source.key not in by_key:
            order.append(source.key)
        by_key[source.key] = source

    for row in data.get("add") or []:
        source = from_dict(row)
        if source.key not in by_key:
            order.append(source.key)
        by_key[source.key] = source

    return tuple(by_key[key] for key in order)


def studios_file(studios: Path | None = None) -> Path | None:
    """Какой файл со студиями будет прочитан на самом деле.

    Порядок: переданный путь, `var/studios.txt`, `config/studios.txt`.
    Вынесено отдельно, чтобы `--list` показывал настоящий файл, а не тот,
    который спросили: разница между «нет файла» и «взят другой» — это
    полчаса на вопрос «почему студии не те».
    """
    for candidate in (studios, BASE_DIR / "var" / STUDIOS_FILE,
                      BASE_DIR / "config" / STUDIOS_FILE):
        if candidate and candidate.exists():
            return candidate
    return None


def load(
    overrides: Path | None = None,
    studios: Path | None = None,
) -> tuple[Source, ...]:
    """Итоговый список источников: встроенные, плюс студии, плюс правки.

    Ошибка в файле — это `SourcesError` с именем файла и строкой, а не
    молчаливый пропуск источника: письмо без раздела заметят через неделю,
    а падение с текстом — в тот же день.
    """
    found = list(SOURCES)

    chosen = studios_file(studios)
    if chosen is None:
        found.extend(default_studios())
    else:
        try:
            found.extend(parse_studios(chosen.read_text(encoding="utf-8")))
        except SourcesError as exc:
            raise SourcesError(f"{chosen}: {exc}") from exc

    if overrides and overrides.exists():
        try:
            data = json.loads(overrides.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SourcesError(f"{overrides}: не читается как JSON — {exc}") from exc
        if not isinstance(data, Mapping):
            raise SourcesError(f"{overrides}: ожидался объект, а не {type(data).__name__}")
        found = list(apply_overrides(found, data))

    for source in found:
        source.check()
    return tuple(found)


def enabled(sources: Sequence[Source]) -> tuple[Source, ...]:
    return tuple(s for s in sources if s.enabled)


def by_section(sources: Sequence[Source]) -> list[tuple[SectionSpec, tuple[Source, ...]]]:
    """Источники, разложенные по разделам в порядке SECTIONS. Раздел без
    включённых источников не возвращается — пустой заголовок в письме хуже,
    чем отсутствие раздела."""
    out: list[tuple[SectionSpec, tuple[Source, ...]]] = []
    for spec in SECTIONS:
        group = tuple(s for s in sources if s.section == spec.key and s.enabled)
        if group:
            out.append((spec, group))
    return out
