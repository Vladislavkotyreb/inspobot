"""Сборка ленты: пройти источники, добрать картинки, сложить разделы.

Здесь встречаются `fetcher` (сеть) и `harvest` (разбор). Всё, что может
сломаться снаружи, ломается внутри одного источника и остаётся там: у
каждого шага есть след (`Trace`), и `probe` печатает эти следы таблицей.
Раздел без находок в письмо не попадает, письмо из-за него не отменяется.

Обращений к Claude тут нет. Стоимость прогона — трафик.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable, Iterable, Mapping, Sequence

from .fetcher import Fetcher, Page, SHOT_TEMPLATE, shot_url
from .harvest import (
    RawItem,
    from_open_graph,
    link_key,
    open_graph,
    parse_feed,
    parse_json,
    parse_links,
)
from .models import Block, Feed, Find
from .sources import SectionSpec, Source, by_section

# Сборщик, который не ходит по HTTP: на вход источник, сколько надо и что уже
# было, на выход находки. Так в ленту попадает Mobbin — вызовом инструмента
# MCP, — не размазывая знание о нём по всему модулю.
Puller = Callable[[Source, int, frozenset], Awaitable[list[RawItem]]]

log = logging.getLogger(__name__)

# Сколько ссылок разбирать со страницы-списка сверх нужного. Первые ссылки
# в вёрстке — это почти всегда шапка и хлебные крошки, прошедшие регулярку,
# а ещё часть находок отсеется как уже присланная.
OVERSHOOT = 8


@dataclass(frozen=True)
class Trace:
    """След одной попытки: что спрашивали, что ответили, сколько вышло.

    Существует ради `probe`. Утренний прогон пишет то же самое в лог, но
    читать его идут только когда письмо уже пришло странным.
    """

    source: str
    way: str
    url: str
    status: int = 0
    found: int = 0
    kept: int = 0
    error: str = ""
    sample: str = ""

    def line(self) -> str:
        head = f"{self.source:<24} {self.way:<5} "
        if self.error:
            return head + f"✗ {self.error}"
        return head + (
            f"✓ HTTP {self.status}, ссылок {self.found}, взято {self.kept}"
            + (f" — {self.sample}" if self.sample else "")
        )


def harvest_page(source: Source, way: str, page: Page) -> tuple[RawItem, ...]:
    """Разобрать ответ тем способом, который его запрашивал."""
    base = source.base() or page.url
    if way == "rss":
        return parse_feed(page.body, base)
    if way == "json":
        return parse_json(
            page.body, source.items_path, source.fields, source.url_template, base
        )
    return parse_links(
        page.body,
        base=page.url or base,
        link_re=source.link_re,
        deny_re=source.deny_re,
        limit=source.limit + OVERSHOOT,
    )


def fresh(items: Iterable[RawItem], seen: frozenset[str]) -> list[RawItem]:
    """Выбросить уже присланное и повторы внутри самой страницы."""
    out: list[RawItem] = []
    local: set[str] = set()
    for item in items:
        key = link_key(item.url)
        if not key or key in seen or key in local:
            continue
        local.add(key)
        out.append(item)
    return out


async def enrich(fetcher: Fetcher, item: RawItem) -> RawItem:
    """Дочитать страницу находки и заполнить пустые поля её og-тегами.

    Именно пустые: заголовок из списка — это название работы, а og-заголовок
    сплошь и рядом «Behance :: Photos, videos, logos» на всю площадку.
    """
    page = await fetcher.get(item.url)
    if not page.ok:
        return item
    return item.filled(**from_open_graph(page.url, open_graph(page.body)))


async def take(
    fetcher: Fetcher,
    source: Source,
    *,
    need: int,
    seen: frozenset[str],
    enrich_pages: bool = True,
    shots: bool = True,
    shot_template: str = SHOT_TEMPLATE,
    pullers: Mapping[str, Puller] | None = None,
) -> tuple[list[Find], list[Trace]]:
    """Находки одного источника: способы по очереди, до первого удачного."""
    traces: list[Trace] = []
    if need <= 0:
        return [], traces

    pullers = pullers or {}
    items: list[RawItem] = []
    for way in source.ways:
        url = source.way_url(way)
        if way in pullers:
            try:
                raw = tuple(await pullers[way](source, need, seen))
            except Exception as exc:  # noqa: BLE001 — чужой сбой не отменяет письмо
                detail = f"{type(exc).__name__}" + (f": {exc}" if str(exc) else "")
                traces.append(Trace(source.key, way, url, error=detail))
                continue
            status = 200
        else:
            page = await fetcher.get(url, source.headers)
            if not page.ok:
                traces.append(Trace(source.key, way, url, page.status, error=page.why()))
                continue
            raw = harvest_page(source, way, page)
            status = page.status
        picked = fresh(raw, seen)[: min(need, source.limit)]
        traces.append(
            Trace(
                source.key,
                way,
                url,
                status,
                found=len(raw),
                kept=len(picked),
                sample=picked[0].url if picked else "",
            )
        )
        if picked:
            items = picked
            break

    if not items:
        return [], traces

    if enrich_pages and source.enrich:
        items = list(await asyncio.gather(*(enrich(fetcher, item) for item in items)))

    finds: list[Find] = []
    for item in items:
        image = item.image
        if not image and shots and source.shot:
            image = shot_url(item.url, shot_template)
        origin = item.tags[0] if source.origin_from_tag and item.tags else source.title
        finds.append(
            Find(
                source=source.key,
                origin=origin,
                url=item.url,
                title=item.title or item.url,
                author=item.author,
                image=image,
                summary=item.summary,
                published=item.published,
                likes=item.likes,
                tags=item.tags,
            )
        )
    return finds, traces


def rotate(sources: Sequence[Source], day: date) -> list[Source]:
    """Сдвинуть порядок источников в разделе по дню.

    Без этого раздел на четыре места, в котором десять студий, каждый день
    показывал бы первые четыре: память не даёт повториться кейсу, но не
    мешает одной и той же студии занимать место вечно.
    """
    if len(sources) < 2:
        return list(sources)
    start = day.toordinal() % len(sources)
    return list(sources[start:]) + list(sources[:start])


async def section_finds(
    fetcher: Fetcher,
    spec: SectionSpec,
    sources: Sequence[Source],
    day: date,
    *,
    seen: frozenset[str],
    **options: object,
) -> tuple[Block, list[Trace]]:
    """Раздел целиком: источники по кругу, пока не наберётся `spec.limit`.

    Источники перебираются по очереди, а не все сразу: набралось с первых
    трёх студий — до остальных семи дело не доходит, и это семь несделанных
    запросов каждое утро.
    """
    finds: list[Find] = []
    traces: list[Trace] = []
    for source in rotate(sources, day):
        if len(finds) >= spec.limit:
            break
        got, trace = await take(
            fetcher, source, need=spec.limit - len(finds), seen=seen, **options
        )
        finds.extend(got)
        traces.extend(trace)
    return Block(spec=spec, finds=tuple(finds[: spec.limit])), traces


def drop_repeats(blocks: Sequence[Block]) -> tuple[Block, ...]:
    """Один и тот же адрес не должен попасть в два раздела.

    Проход намеренно последовательный и в порядке разделов: разделы
    собираются параллельно, и если вычищать повторы прямо там, состав письма
    будет зависеть от того, кто первым ответил.
    """
    used: set[str] = set()
    out: list[Block] = []
    for block in blocks:
        kept: list[Find] = []
        for find in block.finds:
            key = link_key(find.url)
            if key in used:
                continue
            used.add(key)
            kept.append(find)
        if kept:
            out.append(Block(spec=block.spec, finds=tuple(kept)))
    return tuple(out)


async def collect(
    sources: Sequence[Source],
    fetcher: Fetcher,
    day: date,
    *,
    seen: Iterable[str] = (),
    enrich_pages: bool = True,
    shots: bool = True,
    shot_template: str = SHOT_TEMPLATE,
    pullers: Mapping[str, Puller] | None = None,
) -> tuple[Feed, tuple[Trace, ...]]:
    groups = by_section(sources)
    if not groups:
        return Feed(day=day, blocks=()), ()
    known = frozenset(seen)
    results = await asyncio.gather(
        *(
            section_finds(
                fetcher,
                spec,
                group,
                day,
                seen=known,
                enrich_pages=enrich_pages,
                shots=shots,
                shot_template=shot_template,
                pullers=pullers,
            )
            for spec, group in groups
        )
    )
    blocks = drop_repeats([block for block, _ in results])
    traces = tuple(t for _, trace in results for t in trace)
    log.info(
        "Лента на %s: разделов %d, находок %d",
        day.isoformat(),
        len(blocks),
        sum(len(b.finds) for b in blocks),
    )
    return Feed(day=day, blocks=blocks), traces
