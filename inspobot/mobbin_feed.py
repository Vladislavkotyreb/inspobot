"""Раздел ленты, который приносит Mobbin — напрямую, без Claude.

Честно про заголовок. У поиска Mobbin через MCP нет ни сортировки по
популярности, ни окна «за неделю»: инструмент принимает `query`, `platform`,
`limit`, `page`, `exclude_screen_ids` и `mode` — и всё. Поэтому «самое
популярное за неделю» отсюда не запросить, и раздел так не называется.

Что есть на самом деле: тема недели. Каждую неделю берётся следующая
формулировка из списка ниже, Mobbin возвращает лучшее по ней своим
ранжированием, уже показанное исключается. Это ровно та же оговорка, что в
README про B2B/B2C: разделение сделано формулировками запроса, а не фильтром,
которого у Mobbin нет.

Ротация — от номера недели, без состояния на диске: та же идея, что в
`catalog.py`, только список один и общий.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Mapping, Sequence

from .harvest import RawItem
from .mcp_client import McpClient, McpError, McpUnauthorized
from .sources import Source

log = logging.getLogger(__name__)

TOOL = "search_screens"
# Одна и та же строка на все вызовы — этого требует описание инструмента.
TASK_INTENT = "Assemble a daily Telegram digest of notable product UI screens."
# Предел Mobbin на список исключений.
MAX_EXCLUDE = 100
# «standard» вместо «deep» намеренно: deep запускает на стороне Mobbin
# перебор с оценкой каждого кандидата — это долгий поток, который любит
# обрываться, а нам нужен короткий предсказуемый ответ раз в сутки.
MODE = "standard"

SCREEN_ID = re.compile(
    r"mobbin\.com/screens/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)


@dataclass(frozen=True)
class Theme:
    """Тема недели: русская подпись и то, что уходит в Mobbin дословно."""

    title: str
    query: str
    platform: str = "ios"


# Формулировки намеренно описывают экран, а не настроение: у поиска Mobbin
# «современный чистый дизайн» не значит ничего, а «шторка снизу с вариантами
# маршрута» значит.
THEMES: tuple[Theme, ...] = (
    Theme(
        "Главные экраны",
        "flagship consumer app home screen with bold typography, large imagery and clear hierarchy",
    ),
    Theme(
        "Плееры",
        "music and video player screen with cover art, scrubber and now playing controls",
    ),
    Theme(
        "Банки",
        "banking app home with balance, card carousel and recent transactions",
    ),
    Theme(
        "Путешествия",
        "travel booking search with map view, date range picker and price filters",
    ),
    Theme(
        "Доставка",
        "food delivery order tracking with live map, courier status and step timeline",
    ),
    Theme(
        "Камера и съёмка",
        "camera and story composer with capture controls, filters and an effects tray",
    ),
    Theme(
        "Здоровье и спорт",
        "fitness and health dashboard with activity rings, weekly chart and trends",
    ),
    Theme(
        "Карточки товара",
        "ecommerce product page with full bleed photography, variant picker and a sticky buy bar",
        platform="web",
    ),
    Theme(
        "Чаты с ИИ",
        "AI assistant chat with prompt input, streaming answer, sources and follow up suggestions",
    ),
    Theme(
        "Карты и навигация",
        "maps and navigation screen with a bottom sheet, route options and turn by turn preview",
    ),
)


def theme_for(day: date) -> Theme:
    """Тема недели. Понедельник и воскресенье одной недели дают одну тему —
    иначе «тема недели» менялась бы каждый день и называлась бы неправдой."""
    return THEMES[day.isocalendar().week % len(THEMES)]


def screen_ids(seen: Iterable[str], limit: int = MAX_EXCLUDE) -> list[str]:
    """Идентификаторы экранов из уже показанных ссылок.

    Память ленты хранит адреса, а Mobbin исключает по id — вытаскиваем их
    обратно из `mobbin.com/screens/<uuid>`. Список ограничен сотней: столько
    принимает инструмент, и лишнее он не проигнорирует, а отвергнет запрос.
    """
    found: list[str] = []
    for key in seen:
        match = SCREEN_ID.search(key)
        if match:
            found.append(match.group(1).lower())
            if len(found) >= limit:
                break
    return found


def to_items(payload: object, theme: Theme) -> tuple[RawItem, ...]:
    """Ответ инструмента → находки.

    Поля взяты с живого ответа: `id`, `image_url`, `mobbin_url`, `app_name`,
    `platform`. Ни лайков, ни даты Mobbin не отдаёт — поэтому их и нет в
    карточке, а не «нет пока».
    """
    if not isinstance(payload, Mapping):
        return ()
    rows = payload.get("screens") or payload.get("results") or []
    if not isinstance(rows, Sequence):
        return ()
    items: list[RawItem] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        url = str(row.get("mobbin_url") or "")
        if not url:
            continue
        items.append(
            RawItem(
                url=url,
                title=str(row.get("app_name") or "").strip(),
                image=str(row.get("image_url") or ""),
                summary="",
                tags=(theme.title,),
            )
        )
    return tuple(items)


# --- токен ------------------------------------------------------------------


def fresh_token(token_file: Path, mcp_url: str, override: str = "") -> str:
    """Токен, обновлённый принудительно и записанный на диск.

    Запись обязательна. Supabase гасит прежний refresh-токен при каждом
    обмене: обновиться в памяти и не сохранить — значит оставить на диске
    уже погашенный и завтра получить `refresh_token_already_used`, который
    лечится только входом через браузер.
    """
    if override:
        raise McpError(
            "Задан MOBBIN_ACCESS_TOKEN — обновлять нечего. Уберите переменную, "
            "чтобы бот обновлял токен сам, или подставьте свежий вручную."
        )
    from .mobbin_auth import load_tokens, refresh, save_tokens

    tokens = load_tokens(token_file)
    if tokens is None:
        raise McpError(
            f"Нет файла с токеном ({token_file}). Пройдите вход: "
            "python -m inspobot.auth_cli"
        )
    tokens = refresh(tokens, mcp_url)
    save_tokens(token_file, tokens)
    return tokens.access_token


async def search(
    url: str,
    token: str,
    theme: Theme,
    *,
    limit: int,
    exclude: Sequence[str] = (),
    transport: object | None = None,
) -> tuple[RawItem, ...]:
    async with McpClient(url=url, token=token, transport=transport) as client:
        await client.start()
        payload = await client.call_tool(
            TOOL,
            {
                "query": theme.query,
                "platform": theme.platform,
                "limit": max(1, min(limit, 30)),
                "mode": MODE,
                "image_format": "jpg",
                "task_intent": TASK_INTENT,
                **({"exclude_screen_ids": list(exclude)} if exclude else {}),
            },
        )
    return to_items(payload, theme)


Puller = Callable[[Source, int, frozenset], Awaitable[list[RawItem]]]


def puller(
    mcp_url: str,
    token_file: Path,
    access_token: str,
    day: date,
    get_token: Callable[[], str] | None = None,
    transport: object | None = None,
) -> Puller:
    """Замыкание для `gather`: источник, сколько надо, что уже было.

    Один повтор при 401 — и только он. Токен живёт час, а прогон идёт
    минуты, так что истечение прямо посреди запроса редко, но возможно;
    зациклиться на обновлении при этом нельзя.
    """

    async def pull(source: Source, need: int, seen: frozenset) -> list[RawItem]:
        theme = theme_for(day)
        if get_token is None:
            from .mobbin_auth import get_access_token

            token = get_access_token(token_file, mcp_url, access_token)
        else:
            token = get_token()
        exclude = screen_ids(seen)
        log.info(
            "Mobbin: тема недели «%s», нужно %d, исключено %d",
            theme.title,
            need,
            len(exclude),
        )
        # С запасом: часть вернувшегося отсеется памятью, а второй заход
        # стоит ещё одно рукопожатие.
        want = min(30, need + 4)
        try:
            return list(await search(mcp_url, token, theme, limit=want,
                                     exclude=exclude, transport=transport))
        except McpUnauthorized:
            log.info("Mobbin ответил 401 — обновляю токен и повторяю один раз")
            token = fresh_token(token_file, mcp_url, access_token)
            return list(await search(mcp_url, token, theme, limit=want,
                                     exclude=exclude, transport=transport))

    return pull
