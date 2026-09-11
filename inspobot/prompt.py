"""Промпт, схема ответа и разбор — без обращений к сети.

Всё, что можно проверить тестом, живёт здесь; в curator.py остаётся только
вызов Messages API. Один запрос собирает весь дайджест: модель по очереди
дёргает нужный инструмент Mobbin для каждого блока и возвращает единый JSON.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Sequence

from .catalog import Topic
from .models import Digest, Pick, Section
from .profile import Slot

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

TOOL_BY_KIND = {"s": "search_screens", "f": "search_flows", "w": "search_sections"}

# Потолки инструментов Mobbin: screens и sections до 30, flows до 10.
MAX_LIMIT = {"s": 30, "f": 10, "w": 30}

KIND_WORD = {"s": "экранов", "f": "флоу", "w": "секций"}

PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slot": {"type": "integer"},
        "platform": {"type": "string", "enum": ["ios", "web"]},
        "screen_id": {"type": "string"},
        "mobbin_url": {"type": "string"},
        "image_url": {"type": "string"},
        "app_name": {"type": "string"},
        "pattern": {"type": "string"},
        "note": {"type": "string"},
        "screens": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": [
        "slot", "platform", "screen_id", "mobbin_url", "image_url",
        "app_name", "pattern", "note", "screens", "score",
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
2. Обязательно посмотри на сами изображения и отбирай по картинке,
   а не по названию приложения.
3. Отбирай разнообразно: не больше одного результата от одного приложения
   в пределах блока.
4. Комментарий (`note`) пиши по-русски, 1–2 предложения, про конкретное
   решение: что сделано и почему это работает. Без общих слов вроде
   «чистый современный дизайн». Для флоу опиши устройство сценария:
   сколько шагов и что решает каждый.
5. `pattern` — короткая метка приёма на русском, 2–4 слова.
6. Если инструмент вернул меньше подходящего, чем просили, — верни меньше.
   Добивать слабыми вариантами нельзя.
7. `slot` — номер блока, для которого найден результат. Не путай блоки.
8. `screens` — только для флоу: сложи туда ссылки на превью ВСЕХ шагов
   сценария из ответа инструмента, по порядку, ничего не пропуская. Именно
   по ним человек поймёт, как устроен сценарий. Для отдельных экранов и
   секций оставь пустой массив.
9. Во всех вызовах инструментов передавай image_format="jpg".
10. `score` — насколько находка примечательна, от 1 до 10. Это не «нравится
    ли», а «стоит ли к этому вернуться через месяц»: 9–10 — редкий приём,
    который хочется утащить в свой проект; 5–6 — добротно, но встречается
    везде; 1–3 — попало в подборку за неимением лучшего. Не ставь всем
    одинаково: по этим оценкам потом собирается топ за неделю и месяц.
"""

DIGEST_TEMPLATE = """\
Собери подборку на {day}. В ней {count} {blocks}.

{sections}
В `summary` — одно-два предложения по-русски: что объединяет сегодняшнюю
подборку и на что смотреть в первую очередь.

Верни JSON по заданной схеме: в `picks` результаты всех блоков подряд,
у каждого проставлен свой `slot`.
"""

SECTION_TEMPLATE = """\
Блок {index} — {title}, тема «{topic}».
{call}
Отбери лучшие {count}, у каждого поставь slot={index} и platform="{platform}".{screens_note}
"""


SCREENS_NOTE = (
    "\nУ каждого флоу заполни `screens` — превью всех шагов по порядку."
)


class CuratorError(RuntimeError):
    pass


def _plural_blocks(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "блок"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "блока"
    return "блоков"


def search_limit(kind: str, count: int) -> int:
    """Сколько кандидатов просить: с запасом на отбор, но в пределах API."""
    return min(MAX_LIMIT[kind], max(count * 3, 8))


def _tool_call(slot: Slot, topic: Topic, seen: Sequence[str]) -> str:
    tool = TOOL_BY_KIND[slot.kind]
    limit = search_limit(slot.kind, slot.count)
    if slot.kind == "w":
        return f'Вызови {tool} с query="{topic.query}", limit={limit}, image_format="jpg".'
    call = (
        f'Вызови {tool} с query="{topic.query}", platform="{slot.api_platform}", '
        f'limit={limit}, image_format="jpg"'
    )
    if slot.kind == "s":
        call += ', mode="deep"'
        if seen:
            listed = ", ".join(f'"{i}"' for i in seen)
            call += f", exclude_screen_ids=[{listed}]"
    return call + "."


def build_digest_messages(
    day: date,
    plan: Sequence[tuple[Slot, Topic]],
    seen_by_platform: dict[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    seen_by_platform = seen_by_platform or {}
    blocks = []
    for index, (slot, topic) in enumerate(plan, start=1):
        blocks.append(
            SECTION_TEMPLATE.format(
                index=index,
                title=slot.title(),
                topic=topic.title,
                call=_tool_call(slot, topic, seen_by_platform.get(slot.api_platform, ())),
                count=slot.count,
                platform=slot.api_platform,
                screens_note=SCREENS_NOTE if slot.kind == "f" else "",
            )
        )
    prompt = DIGEST_TEMPLATE.format(
        day=day.isoformat(),
        count=len(plan),
        blocks=_plural_blocks(len(plan)),
        sections="\n".join(blocks),
    )
    return [{"role": "user", "content": prompt}]


def is_valid_pick(pick: dict[str, Any], kind: str = "s") -> bool:
    """Отсекает выдуманное: ссылка обязана быть с mobbin.com.

    У экранов id — заведомо uuid, это дополнительная проверка. У флоу и секций
    формат id живым ответом не подтверждён, поэтому там требуется лишь непустое
    значение: строгая проверка молча выбрасывала бы всё подряд.
    """
    ident = str(pick.get("screen_id", ""))
    if pick.get("platform") not in {"ios", "web"}:
        return False
    if kind == "s" and not UUID_RE.match(ident):
        return False
    if not ident.strip():
        return False
    return str(pick.get("mobbin_url", "")).startswith(
        "https://mobbin.com/"
    ) and str(pick.get("image_url", "")).startswith("https://")


MAX_FLOW_SCREENS = 24
DEFAULT_SCORE = 5


def _score_from(item: dict[str, Any]) -> int:
    """Оценка 1–10; всё невнятное превращается в середину шкалы."""
    try:
        value = int(item.get("score", DEFAULT_SCORE))
    except (TypeError, ValueError):
        return DEFAULT_SCORE
    return max(1, min(10, value))


def _screens_from(item: dict[str, Any]) -> tuple[str, ...]:
    """Шаги флоу: только внятные ссылки, по порядку, без повторов."""
    raw = item.get("screens")
    if not isinstance(raw, list):
        return ()
    urls: list[str] = []
    for value in raw:
        url = str(value).strip()
        if url.startswith("https://") and url not in urls:
            urls.append(url)
    return tuple(urls[:MAX_FLOW_SCREENS])


def _pick_from(item: dict[str, Any]) -> Pick:
    return Pick(
        platform=str(item["platform"]),
        screen_id=str(item["screen_id"]),
        mobbin_url=str(item["mobbin_url"]),
        image_url=str(item["image_url"]),
        app_name=str(item.get("app_name", "")).strip() or "—",
        pattern=str(item.get("pattern", "")).strip(),
        note=str(item.get("note", "")).strip(),
        screens=_screens_from(item),
        score=_score_from(item),
    )


def parse_digest(
    raw: str,
    day: date,
    plan: Sequence[tuple[Slot, Topic]],
    seen: set[str] | None = None,
    title: str = "",
) -> Digest:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CuratorError(f"Ответ модели — не JSON: {exc}") from exc

    seen = seen or set()
    used: set[str] = set()
    by_slot: dict[int, list[Pick]] = {i: [] for i in range(1, len(plan) + 1)}

    for item in data.get("picks", []):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("slot", 0))
        except (TypeError, ValueError):
            continue
        if index not in by_slot:
            continue
        slot, _ = plan[index - 1]
        if not is_valid_pick(item, slot.kind):
            continue
        ident = str(item["screen_id"])
        if ident in used or ident in seen:
            continue
        used.add(ident)
        by_slot[index].append(_pick_from(item))

    sections = tuple(
        Section(slot=slot, topic=topic, picks=tuple(by_slot[index]))
        for index, (slot, topic) in enumerate(plan, start=1)
        if by_slot[index]
    )
    if not sections:
        raise CuratorError("Модель не вернула ни одного пригодного результата.")

    return Digest(
        day=day,
        title=title,
        summary=str(data.get("summary", "")).strip(),
        sections=sections,
    )
