"""Форматирование дайджеста для Telegram (parse_mode=HTML).

Чистый слой: на входе данные, на выходе строки. Проверяется тестами.
"""

from __future__ import annotations

from datetime import date
from html import escape

from .models import Digest, Pick, Section

MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

# Иконка блока: экраны различаются по платформе, флоу и секции — по виду.
SCREEN_ICON = {"ios": "📱", "web": "🖥"}
KIND_ICON = {"f": "▶️", "w": "🌐"}

# Telegram: 1024 символа на подпись к фото, 4096 на текстовое сообщение.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def human_date(day: date) -> str:
    # Неразрывный пробел: «2 сентября» не должно переноситься по строкам.
    return f"{day.day} {MONTHS_GENITIVE[day.month - 1]}"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _steps_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "шаг"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "шага"
    return "шагов"


def section_icon(section: Section) -> str:
    if section.slot.kind in KIND_ICON:
        return KIND_ICON[section.slot.kind]
    return SCREEN_ICON.get(section.slot.api_platform, "•")


def header_html(digest: Digest) -> str:
    head = f"Референсы на {escape(human_date(digest.day))}"
    if digest.title:
        head += f" — {escape(digest.title)}"
    lines = [f"<b>{head}</b>", ""]
    for section in digest.sections:
        lines.append(
            f"{section_icon(section)} {escape(section.title())}"
            f" — {len(section.picks)}"
        )
    if digest.summary:
        lines += ["", escape(digest.summary)]
    return _clip("\n".join(lines), MESSAGE_LIMIT)


def caption_html(section: Section, pick: Pick, index: int, total: int) -> str:
    head = f"{section_icon(section)} {escape(section.topic.title)} · {index}/{total}"
    if pick.screens:
        head += f" · {len(pick.screens)} {_steps_word(len(pick.screens))}"
    parts = [head, f"<b>{escape(pick.app_name)}</b>"]
    if pick.pattern:
        parts.append(escape(pick.pattern))
    if pick.note:
        parts.append(escape(pick.note))
    # Ссылки в тексте нет: она уехала в кнопку под сообщением.
    return _clip("\n".join(parts), CAPTION_LIMIT)


OPEN_LABEL = "Открыть на Mobbin"


def pick_keyboard(pick: Pick) -> dict:
    """Кнопка под находкой вместо ссылки в тексте."""
    return {"inline_keyboard": [[{"text": OPEN_LABEL, "url": pick.mobbin_url}]]}
