"""Форматирование подборки для Telegram (parse_mode=HTML).

Чистый слой: на входе данные, на выходе строки. Проверяется тестами.
"""

from __future__ import annotations

from datetime import date
from html import escape

from .models import Digest, Pick

MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

PLATFORM_ICON = {"ios": "📱", "web": "🖥"}

# Telegram: 1024 символа на подпись к фото, 4096 на текстовое сообщение.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def human_date(day: date) -> str:
    # Неразрывный пробел: «2 сентября» не должно переноситься по строкам.
    return f"{day.day}\u00a0{MONTHS_GENITIVE[day.month - 1]}"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def header_html(digest: Digest) -> str:
    lines = [
        f"<b>Референсы на {escape(human_date(digest.day))}</b>",
        "",
        f"{PLATFORM_ICON['ios']} {escape(digest.mobile_topic.title)}",
        f"{PLATFORM_ICON['web']} {escape(digest.desktop_topic.title)}",
    ]
    if digest.summary:
        lines += ["", escape(digest.summary)]
    return _clip("\n".join(lines), MESSAGE_LIMIT)


def caption_html(pick: Pick, index: int, total: int) -> str:
    icon = PLATFORM_ICON.get(pick.platform, "•")
    head = f"{icon} {index}/{total} · <b>{escape(pick.app_name)}</b>"
    parts = [head]
    if pick.pattern:
        parts.append(escape(pick.pattern))
    if pick.note:
        parts.append(escape(pick.note))
    parts.append(f'<a href="{escape(pick.mobbin_url, quote=True)}">Открыть на Mobbin</a>')
    return _clip("\n".join(parts), CAPTION_LIMIT)
