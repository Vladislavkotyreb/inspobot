"""Пошаговый выбор референсов: чистая машина состояний.

Состояние целиком лежит в `callback_data` кнопки, а не в базе. Причин две:
нажатие на старое сообщение работает даже после перезапуска бота, и нечего
чистить — не бывает брошенных наполовину сессий. Telegram даёт под
`callback_data` 64 байта, поэтому коды односимвольные: `w|s|i|b|onboarding`.

Кнопка «назад» — это тот же формат с очищенным последним полем, так что
отдельной ветки в обработчике не нужно: пришёл неполный выбор — показываем
шаг, пришёл полный — идём искать.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .catalog import (
    AUDIENCES,
    KIND_HINTS,
    KINDS,
    KINDS_WITHOUT_PLATFORM,
    PLATFORM_API,
    PLATFORMS,
    Option,
    Topic,
    find_topic,
    label,
    topics_for,
)

PREFIX = "w"
EMPTY = "-"
SEPARATOR = "|"
CALLBACK_LIMIT = 64  # ограничение Telegram на callback_data

STEP_KIND = "kind"
STEP_PLATFORM = "platform"
STEP_AUDIENCE = "audience"
STEP_TOPIC = "topic"

ORDER = (STEP_KIND, STEP_PLATFORM, STEP_AUDIENCE, STEP_TOPIC)

PROMPTS = {
    STEP_KIND: "Что ищем?",
    STEP_PLATFORM: "Платформа?",
    STEP_AUDIENCE: "Аудитория?",
    STEP_TOPIC: "Тема?",
}


@dataclass(frozen=True)
class Selection:
    kind: str = ""
    platform: str = ""
    audience: str = ""
    topic: str = ""

    @property
    def needs_platform(self) -> bool:
        return self.kind not in KINDS_WITHOUT_PLATFORM


def next_step(selection: Selection) -> str | None:
    """Первый незаполненный шаг или None, когда выбор готов."""
    if not selection.kind:
        return STEP_KIND
    if selection.needs_platform and not selection.platform:
        return STEP_PLATFORM
    if not selection.audience:
        return STEP_AUDIENCE
    if not selection.topic:
        return STEP_TOPIC
    return None


def is_complete(selection: Selection) -> bool:
    return next_step(selection) is None


def with_value(selection: Selection, step: str, value: str) -> Selection:
    return replace(selection, **{step: value})


def back(selection: Selection) -> Selection | None:
    """Состояние на шаг назад; None — если отступать уже некуда."""
    filled = [
        step
        for step in ORDER
        if getattr(selection, step)
        and (step != STEP_PLATFORM or selection.needs_platform)
    ]
    if not filled:
        return None
    return replace(selection, **{filled[-1]: ""})


def options(step: str, selection: Selection) -> tuple[Option, ...]:
    if step == STEP_KIND:
        return KINDS
    if step == STEP_PLATFORM:
        return PLATFORMS
    if step == STEP_AUDIENCE:
        return AUDIENCES
    if step == STEP_TOPIC:
        return tuple(
            Option(topic.slug, topic.title)
            for topic in topics_for(selection.kind, selection.audience)
        )
    raise ValueError(f"неизвестный шаг: {step}")


# --- кодирование для кнопок -------------------------------------------------


def encode(selection: Selection) -> str:
    parts = [getattr(selection, step) or EMPTY for step in ORDER]
    data = SEPARATOR.join([PREFIX, *parts])
    if len(data.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError(f"callback_data длиннее {CALLBACK_LIMIT} байт: {data}")
    return data


def decode(data: str) -> Selection | None:
    """Разбор callback_data. None — если это не наша кнопка или коды чужие."""
    parts = data.split(SEPARATOR)
    if len(parts) != len(ORDER) + 1 or parts[0] != PREFIX:
        return None
    values = ["" if p == EMPTY else p for p in parts[1:]]
    selection = Selection(*values)
    return selection if _valid(selection) else None


def _valid(selection: Selection) -> bool:
    codes = {option.code for option in KINDS}
    if selection.kind and selection.kind not in codes:
        return False
    if selection.platform:
        if not selection.needs_platform:
            return False
        if selection.platform not in {option.code for option in PLATFORMS}:
            return False
    if selection.audience and selection.audience not in {o.code for o in AUDIENCES}:
        return False
    if selection.topic:
        if not (selection.kind and selection.audience):
            return False
        if find_topic(selection.kind, selection.audience, selection.topic) is None:
            return False
    return True


# --- то, что уходит в поиск -------------------------------------------------


def topic_of(selection: Selection) -> Topic | None:
    if not (selection.kind and selection.audience and selection.topic):
        return None
    return find_topic(selection.kind, selection.audience, selection.topic)


def api_platform(selection: Selection) -> str:
    """Значение параметра platform для Mobbin. Секции сайта всегда веб."""
    if not selection.needs_platform:
        return "web"
    return PLATFORM_API[selection.platform]


def breadcrumb(selection: Selection) -> str:
    """Что уже выбрано — строкой для человека."""
    parts: list[str] = []
    if selection.kind:
        parts.append(label(KINDS, selection.kind))
    if selection.platform:
        parts.append(label(PLATFORMS, selection.platform))
    if selection.audience:
        parts.append(label(AUDIENCES, selection.audience))
    topic = topic_of(selection)
    if topic:
        parts.append(topic.title)
    return " · ".join(parts)


def prompt(step: str, selection: Selection) -> str:
    text = PROMPTS[step]
    if step == STEP_KIND:
        hints = "\n".join(
            f"{option.label} — {KIND_HINTS[option.code]}" for option in KINDS
        )
        return f"{text}\n\n{hints}"
    if step == STEP_AUDIENCE:
        return (
            f"{text}\n\nУ Mobbin нет такого фильтра, поэтому деление задаётся "
            "формулировкой запроса: у B2B спрашиваем про биллинг и роли, "
            "у B2C — про пейволл и ленту."
        )
    return text
