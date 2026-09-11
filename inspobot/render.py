"""Форматирование дайджеста для Telegram (parse_mode=HTML).

Чистый слой: на входе данные, на выходе строки. Проверяется тестами.
"""

from __future__ import annotations

from datetime import date
from html import escape

from .models import Digest, Pick, Section
from .state import StoredPick

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


def pick_keyboard(pick: "Pick | StoredPick") -> dict:
    """Кнопка под находкой вместо ссылки в тексте."""
    return {"inline_keyboard": [[{"text": OPEN_LABEL, "url": pick.mobbin_url}]]}


# --- топ за период ----------------------------------------------------------

TOP_WEEK = "top:week"
TOP_MONTH = "top:month"
PERIOD_LABEL = {"week": "за неделю", "month": "за месяц"}


def digest_keyboard() -> dict:
    """Две кнопки под шапкой дайджеста. Это callback-кнопки: без
    ретранслятора, который принимает нажатия, они ничего не сделают —
    поэтому добавляются только при INSPOBOT_TOP_BUTTONS=1."""
    return {
        "inline_keyboard": [
            [
                {"text": "Топ-10 за неделю", "callback_data": TOP_WEEK},
                {"text": "Топ-10 за месяц", "callback_data": TOP_MONTH},
            ]
        ]
    }


def top_header_html(period: str, since: date, until: date, count: int) -> str:
    label = PERIOD_LABEL.get(period, period)
    head = f"<b>Топ-{count} {escape(label)}</b>"
    span = f"{escape(human_date(since))} — {escape(human_date(until))}"
    if count == 0:
        return f"{head}\n\nЗа {span} ничего не присылалось — топ пуст."
    return f"{head}\n{span}\n\nЛучшее из того, что уже было в подборках — по оценке в момент отбора."


def top_caption_html(rank: int, pick: StoredPick) -> str:
    parts = [
        f"<b>#{rank}</b> · {pick.score}/10 · {escape(pick.topic or pick.block)}",
        f"<b>{escape(pick.app_name)}</b>",
    ]
    if pick.pattern:
        parts.append(escape(pick.pattern))
    if pick.note:
        parts.append(escape(pick.note))
    parts.append(f"<i>{escape(human_date(pick.day))}</i>")
    return _clip("\n".join(parts), CAPTION_LIMIT)
