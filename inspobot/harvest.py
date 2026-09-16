"""Разбор того, что вернул источник: текст на входе — находки на выходе.

Ни сети, ни httpx, ни SQLite — поэтому весь разбор проверяется тестами на
сохранённых страницах, без единого запроса наружу. Сетевой слой живёт
отдельно, в `fetcher.py`, и знает только про «скачать адрес».

Способов разбора три, и они намеренно разной степени наглости:

* `rss` — разбор ленты. Самый честный: сайт сам отдаёт машиночитаемое.
* `json` — внутренняя ручка сайта, отвечающая JSON. Хрупко к переименованию
  полей, но переживает любую перевёрстку.
* `html` — выгрести со страницы все ссылки, подходящие под регулярку, и
  дочитать заголовок с картинкой из og-тегов самой находки.

Последний способ здесь главный, и это осознанно. Полноценный разбор вёрстки
семи разных сайтов — семь парсеров, которые ломаются семью способами. Одна
регулярка на ссылки плюс общие для всего веба og-теги дают то же самое, а
чинятся правкой одной строки в `sources.py` (или в `var/sources.json`, вообще
без выката).
"""

from __future__ import annotations

import html as html_entities
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree


@dataclass(frozen=True)
class RawItem:
    """Находка, как её удалось вытащить из источника.

    Пустые поля — норма: у ссылки со страницы-списка сначала есть только
    адрес, остальное дочитывается со страницы находки (см. `open_graph`).
    """

    url: str
    title: str = ""
    author: str = ""
    image: str = ""
    summary: str = ""
    published: date | None = None
    likes: int = 0
    tags: tuple[str, ...] = ()

    def filled(self, **changes: Any) -> "RawItem":
        """Дополнить пустые поля, не затирая уже известные.

        Порядок важен: со страницы-списка часто приходит точный заголовок и
        имя автора, а og-теги целевой страницы — это маркетинговый слоган
        вроде «Awwwards — Site of the Day». Дочитанное не должно вытеснять
        то, что источник сказал про себя сам.
        """
        merged = {
            name: value
            for name, value in changes.items()
            if value and not getattr(self, name)
        }
        return replace(self, **merged) if merged else self


# --- нормализация адреса ----------------------------------------------------

# Метки кампаний. Один и тот же кейс приходит из ленты с utm и со страницы без
# них; без вычистки он показывается дважды и выглядит как сломанная память.
TRACKING = re.compile(r"^(utm_|ga_|yclid$|gclid$|fbclid$|mc_[ce]id$|ref$|ref_src$|igshid$)")


def link_key(url: str) -> str:
    """Ключ для памяти «это уже присылали».

    Схема и хост приводятся к нижнему регистру, `www.` снимается, хвостовой
    слэш убирается, метки кампаний выбрасываются, остаток запроса
    сортируется. Полезный запрос не трогаем: у половины каталогов адрес
    находки — это и есть `?id=…`.
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    # Без хоста это не ссылка, а мусор: пустая строка, «не ссылка», якорь.
    # Вернуть непустой ключ здесь — значит пустить такую находку в письмо и
    # в память: urlunsplit склеивает из пустоты вполне правдоподобное
    # «https:///», и все проверки «ключ есть» его пропускают.
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not TRACKING.match(k)
        )
    )
    scheme = "https" if parts.scheme in ("", "http", "https") else parts.scheme
    return urlunsplit((scheme, host, path, query, ""))


def absolutize(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    return urljoin(base, href)


# --- текст ------------------------------------------------------------------

_BLOCKS = re.compile(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_SPACES = re.compile(r"\s+")


def strip_tags(text: str) -> str:
    return _TAGS.sub(" ", _BLOCKS.sub(" ", text or ""))


def clean_text(text: str, limit: int = 0) -> str:
    """Человекочитаемая строка из чего угодно: без тегов, сущностей и лишних
    пробелов. `limit` режет по границе слова — обрубок посреди слова в
    подписи выглядит как ошибка кодировки."""
    out = _SPACES.sub(" ", html_entities.unescape(strip_tags(text))).strip()
    if limit and len(out) > limit:
        cut = out[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
        out = (cut or out[:limit].rstrip()) + "…"
    return out


def as_int(value: Any) -> int:
    """Число лайков из чего придёт: 1234, "1234", "1.2k", "1 234", None."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip().lower().replace(" ", "").replace(" ", "")
    text = text.replace(",", ".") if re.fullmatch(r"\d+\.?\d*[km]", text) else text.replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", text)
    if not match:
        return 0
    scale = {"": 1, "k": 1000, "m": 1000000}[match.group(2)]
    return int(float(match.group(1)) * scale)


def as_date(value: Any) -> date | None:
    """Дата из RFC 822 (RSS), ISO 8601 (Atom и JSON) или unix-времени."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value)).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,12}", text):
        return as_date(int(text))
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(*(int(g) for g in match.groups()))
        except ValueError:
            return None
    return None


# --- html: мета-теги и ссылки ----------------------------------------------

_META = re.compile(r"(?is)<meta\b([^>]*?)/?>")
_ATTR = re.compile(r"""(?is)([a-z_][\w:.\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""")
_HREF = re.compile(r"""(?is)<a\b([^>]*?)>""")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title\s*>")


def _attrs(chunk: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _ATTR.finditer(chunk):
        name = match.group(1).lower()
        value = next((g for g in match.groups()[1:] if g is not None), "")
        found.setdefault(name, html_entities.unescape(value).strip())
    return found


def open_graph(body: str) -> dict[str, str]:
    """og-, twitter- и обычные мета-теги страницы одной плоской картой.

    Ключи приводятся к нижнему регистру: `og:image`, `twitter:image`,
    `description`, плюс `title` из `<title>`, если своего og-заголовка нет.
    Первое вхождение выигрывает — у страниц с несколькими `og:image` первая
    почти всегда главная, а дальше идут иконки и логотипы.
    """
    tags: dict[str, str] = {}
    for match in _META.finditer(body or ""):
        attrs = _attrs(match.group(1))
        name = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        content = attrs.get("content") or attrs.get("value")
        if name and content:
            tags.setdefault(name.lower(), content)
    title = _TITLE.search(body or "")
    if title:
        tags.setdefault("title", clean_text(title.group(1)))
    return tags


# Что считать картинкой находки, по убыванию доверия.
IMAGE_KEYS = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src", "image")
TITLE_KEYS = ("og:title", "twitter:title", "title")
SUMMARY_KEYS = ("og:description", "twitter:description", "description")
AUTHOR_KEYS = ("article:author", "og:site_name", "author", "twitter:creator")


def from_open_graph(base: str, tags: Mapping[str, str]) -> dict[str, str]:
    """Достроить относительные адреса картинок: `og:image` сплошь и рядом
    отдают как `/img/preview.png`, и такая ссылка в Telegram не открывается."""

    def pick(keys: Sequence[str]) -> str:
        return next((tags[k].strip() for k in keys if tags.get(k, "").strip()), "")

    image = pick(IMAGE_KEYS)
    return {
        "title": clean_text(pick(TITLE_KEYS)),
        "image": absolutize(base, image) if image else "",
        "summary": clean_text(pick(SUMMARY_KEYS), limit=300),
        "author": clean_text(pick(AUTHOR_KEYS), limit=80),
    }


def parse_links(
    body: str,
    base: str,
    link_re: str,
    deny_re: str = "",
    limit: int = 0,
) -> tuple[RawItem, ...]:
    """Все ссылки страницы, подходящие под регулярку, в порядке вёрстки.

    Регулярка примеряется к уже достроенному абсолютному адресу — иначе
    одно и то же правило пришлось бы писать дважды, под `/shots/123` и под
    `https://dribbble.com/shots/123`.

    Текст ссылки забирается как заголовок: на страницах-списках это обычно
    название работы, и оно точнее, чем og-заголовок целевой страницы.
    Картинку отсюда не берём — в списках это ленивые заглушки в 20 пикселей.
    """
    allow = re.compile(link_re) if link_re else None
    deny = re.compile(deny_re) if deny_re else None
    seen: set[str] = set()
    items: list[RawItem] = []
    for match in _HREF.finditer(body or ""):
        attrs = _attrs(match.group(1))
        url = absolutize(base, attrs.get("href", ""))
        if not url or (allow and not allow.search(url)):
            continue
        if deny and deny.search(url):
            continue
        key = link_key(url)
        if key in seen:
            continue
        seen.add(key)
        tail = body[match.end() : match.end() + 400]
        label = clean_text(tail.split("</a", 1)[0], limit=120) if "</a" in tail else ""
        items.append(RawItem(url=url, title=label or clean_text(attrs.get("title", ""), 120)))
        if limit and len(items) >= limit:
            break
    return tuple(items)


# --- json -------------------------------------------------------------------


def dig(data: Any, path: str) -> Any:
    """Значение по точечному пути: `data.projects.0.name`.

    Числовой шаг — индекс списка, `*` — «первый непустой из всех». Ничего не
    нашлось — None, без исключения: у половины ответов половина полей
    необязательна, и падать на этом нельзя.
    """
    if not path:
        return None
    current = data
    for step in path.split("."):
        if current is None:
            return None
        if step == "*":
            values = current.values() if isinstance(current, Mapping) else current
            current = next((v for v in values if v), None) if isinstance(values, Iterable) else None
            continue
        if isinstance(current, Mapping):
            current = current.get(step)
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            try:
                current = current[int(step)]
            except (ValueError, IndexError):
                return None
            continue
        return None
    return current


def first(data: Any, paths: str) -> Any:
    """Первое непустое значение из путей, перечисленных через `|`.

    Сайты переименовывают поля, не предупреждая: `covers.original` было,
    стало `covers.404`. Список альтернатив переживает это молча.
    """
    for path in (p.strip() for p in paths.split("|")):
        value = dig(data, path)
        if value not in (None, "", [], {}):
            return value
    return None


def parse_json(
    body: str,
    items_path: str,
    fields: Mapping[str, str],
    url_template: str = "",
    base: str = "",
) -> tuple[RawItem, ...]:
    """Разбор ответа внутренней ручки сайта по карте полей.

    `url_template` — для тех, кто не отдаёт готовой ссылки, а отдаёт `id` и
    `slug`: `https://www.behance.net/gallery/{id}/{slug}`.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ()
    rows = dig(data, items_path) if items_path else data
    if isinstance(rows, Mapping):
        rows = list(rows.values())
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ()

    items: list[RawItem] = []
    for row in rows:
        values = {name: first(row, path) for name, path in fields.items()}
        url = str(values.get("url") or "")
        if url_template:
            try:
                url = url_template.format(**{k: v for k, v in values.items() if v is not None})
            except (KeyError, IndexError):
                url = url
        url = absolutize(base, url) if base else url
        if not url:
            continue
        tags = values.get("tags") or ()
        if isinstance(tags, (str, bytes)):
            tags = (clean_text(str(tags)),)
        items.append(
            RawItem(
                url=url,
                title=clean_text(str(values.get("title") or ""), 200),
                author=clean_text(str(values.get("author") or ""), 80),
                image=absolutize(base or url, str(values.get("image") or "")),
                summary=clean_text(str(values.get("summary") or ""), 300),
                published=as_date(values.get("published")),
                likes=as_int(values.get("likes")),
                tags=tuple(clean_text(str(t), 40) for t in tags if t)[:6],
            )
        )
    return tuple(items)


# --- rss / atom -------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _kids(node: Any, *names: str) -> list[Any]:
    wanted = {n.lower() for n in names}
    return [child for child in node if _local(child.tag) in wanted]


def _text(node: Any, *names: str) -> str:
    for child in _kids(node, *names):
        value = "".join(child.itertext()).strip()
        if value:
            return value
    return ""


_IMG = re.compile(r"""(?is)<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""")


def _feed_image(node: Any, body: str, base: str) -> str:
    """Картинка записи: enclosure, media:*, потом первая <img> в теле.

    Порядок не случаен. `<img>` из описания часто оказывается счётчиком
    посещений в один пиксель, поэтому она идёт последней и только если
    ничего объявленного явно не нашлось.
    """
    for child in _kids(node, "enclosure"):
        url = child.get("url", "")
        if url and child.get("type", "image").startswith("image"):
            return absolutize(base, url)
    for child in _kids(node, "content", "thumbnail", "group"):
        url = child.get("url", "")
        if url and (child.get("medium") in (None, "image") or "image" in child.get("type", "image")):
            return absolutize(base, url)
        nested = _feed_image(child, "", base)
        if nested:
            return nested
    match = _IMG.search(body or "")
    if match:
        url = next((g for g in match.groups() if g), "")
        return absolutize(base, html_entities.unescape(url))
    return ""


def _feed_link(node: Any, base: str) -> str:
    """Ссылка записи. В Atom их несколько, и нужна `rel="alternate"` —
    `rel="edit"` ведёт в служебную ручку, а не на страницу работы."""
    links = _kids(node, "link")
    for child in links:
        if child.get("rel", "alternate") == "alternate" and child.get("href"):
            return absolutize(base, child.get("href", ""))
    for child in links:
        value = (child.get("href") or "".join(child.itertext())).strip()
        if value:
            return absolutize(base, value)
    guid = _text(node, "guid", "id")
    return absolutize(base, guid) if guid.startswith(("http://", "https://")) else ""


def parse_feed(body: str, base: str = "") -> tuple[RawItem, ...]:
    """RSS 2.0 и Atom одним разбором: различия сводятся к именам тегов.

    Пространства имён игнорируются намеренно — сравниваются только локальные
    имена. Половина лент в природе объявляет media-теги в своём пространстве имён
    или не объявляет вовсе, и строгая сверка URI ломается именно на них.
    """
    try:
        root = ElementTree.fromstring((body or "").strip().lstrip("﻿"))
    except ElementTree.ParseError:
        return ()
    nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    items: list[RawItem] = []
    for node in nodes:
        url = _feed_link(node, base)
        if not url:
            continue
        raw_body = _text(node, "encoded", "description", "summary", "content")
        items.append(
            RawItem(
                url=url,
                title=clean_text(_text(node, "title"), 200),
                author=clean_text(_text(node, "creator", "author", "name"), 80),
                image=_feed_image(node, raw_body, base or url),
                summary=clean_text(raw_body, 300),
                published=as_date(_text(node, "pubdate", "published", "date", "updated")),
                tags=tuple(
                    clean_text("".join(c.itertext()), 40)
                    for c in _kids(node, "category")
                )[:6],
            )
        )
    return tuple(items)
