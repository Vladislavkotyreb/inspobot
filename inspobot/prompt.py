"""Промпт, схема ответа и разбор — без обращений к сети.

Всё, что можно проверить тестом, живёт здесь; в curator.py остаётся только
вызов Messages API.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Sequence

from .models import Digest, Pick
from .topics import Topic

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "platform": {"type": "string", "enum": ["ios", "web"]},
        "screen_id": {"type": "string"},
        "mobbin_url": {"type": "string"},
        "image_url": {"type": "string"},
        "app_name": {"type": "string"},
        "pattern": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": [
        "platform", "screen_id", "mobbin_url", "image_url", "app_name", "pattern", "note",
    ],
    "additionalProperties": False,
}

DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "picks": {"type": "array", "items": PICK_SCHEMA},
    },
    "required": ["summary", "picks"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
Ты — арт-директор, который каждое утро собирает подборку интерфейсных
референсов из Mobbin для продуктового дизайнера.

Правила:
1. Работай только с данными инструментов Mobbin. Ни один экран, ссылку,
   название приложения или id нельзя придумывать или менять — копируй
   значения из ответа инструмента дословно.
2. Обязательно посмотри на сами изображения экранов и отбирай по картинке,
   а не по названию приложения.
3. Отбирай разнообразно: не больше одного экрана от одного приложения
   в пределах подборки.
4. Комментарий (`note`) пиши по-русски, 1–2 предложения, про конкретное
   решение на экране: что сделано и почему это работает. Без общих слов
   вроде «чистый современный дизайн».
5. `pattern` — короткая метка приёма на русском, 2–4 слова.
6. Если инструмент вернул меньше подходящих экранов, чем просили, — верни
   меньше. Добивать слабыми вариантами нельзя.
"""

USER_TEMPLATE = """\
Собери подборку на {day}.

Мобильная тема дня: {mobile_title}.
Запрос для поиска (platform="ios"): {mobile_query}

Десктопная тема дня: {desktop_title}.
Запрос для поиска (platform="web"): {desktop_query}

Что сделать:
1. Вызови search_screens с platform="ios" и мобильным запросом, limit={search_limit},
   mode="deep".{ios_exclude}
2. Вызови search_screens с platform="web" и десктопным запросом, limit={search_limit},
   mode="deep".{web_exclude}
3. Отбери лучшие {picks} экрана для ios и лучшие {picks} для web.
4. В `summary` — одно-два предложения по-русски: что общего в сегодняшней
   подборке и на что посмотреть в первую очередь.

Верни JSON по заданной схеме: поле `picks` — сначала ios, потом web.
"""


class CuratorError(RuntimeError):
    pass


def _exclude_clause(ids: Sequence[str]) -> str:
    if not ids:
        return ""
    listed = ", ".join(f'"{i}"' for i in ids)
    return (
        "\n   Обязательно передай exclude_screen_ids=[" + listed + "] — "
        "эти экраны уже присылали."
    )


def build_messages(
    day: date,
    mobile: Topic,
    desktop: Topic,
    picks: int,
    ios_seen: Sequence[str] = (),
    web_seen: Sequence[str] = (),
) -> list[dict[str, Any]]:
    prompt = USER_TEMPLATE.format(
        day=day.isoformat(),
        mobile_title=mobile.title,
        mobile_query=mobile.query,
        desktop_title=desktop.title,
        desktop_query=desktop.query,
        picks=picks,
        search_limit=min(30, max(picks * 3, 12)),
        ios_exclude=_exclude_clause(ios_seen),
        web_exclude=_exclude_clause(web_seen),
    )
    return [{"role": "user", "content": prompt}]


def is_valid_pick(pick: dict[str, Any]) -> bool:
    """Отсекает выдуманные экраны: id должен быть uuid, ссылка — с mobbin.com."""
    return (
        pick.get("platform") in {"ios", "web"}
        and bool(UUID_RE.match(str(pick.get("screen_id", ""))))
        and str(pick.get("mobbin_url", "")).startswith("https://mobbin.com/")
        and str(pick.get("image_url", "")).startswith("https://")
    )


def parse_digest(
    raw: str,
    day: date,
    mobile: Topic,
    desktop: Topic,
    seen: set[str] | None = None,
) -> Digest:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CuratorError(f"Ответ модели — не JSON: {exc}") from exc

    seen = seen or set()
    picks: list[Pick] = []
    used_ids: set[str] = set()
    for item in data.get("picks", []):
        if not isinstance(item, dict) or not is_valid_pick(item):
            continue
        screen_id = str(item["screen_id"])
        if screen_id in used_ids or screen_id in seen:
            continue
        used_ids.add(screen_id)
        picks.append(
            Pick(
                platform=str(item["platform"]),
                screen_id=screen_id,
                mobbin_url=str(item["mobbin_url"]),
                image_url=str(item["image_url"]),
                app_name=str(item.get("app_name", "")).strip() or "—",
                pattern=str(item.get("pattern", "")).strip(),
                note=str(item.get("note", "")).strip(),
            )
        )

    if not picks:
        raise CuratorError("Модель не вернула ни одного пригодного экрана.")

    return Digest(
        day=day,
        mobile_topic=mobile,
        desktop_topic=desktop,
        summary=str(data.get("summary", "")).strip(),
        picks=tuple(picks),
    )
