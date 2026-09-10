"""Настройка расписания: `python -m inspobot.setup_cli`.

Проходится в терминале, столько раз, сколько нужно. Результат — файл с
расписанием на неделю, по которому ежедневный запуск собирает письмо.
Никакого процесса между запусками не живёт: продукт — дайджест, а не бот.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from .catalog import AUDIENCES, KIND_HINTS, KINDS, KINDS_WITHOUT_PLATFORM, PLATFORMS, Option
from .catalog import topics_for
from .config import Config
from .profile import (
    DEFAULT_COUNT,
    MAX_COUNT,
    WEEKDAY_NAMES,
    Day,
    ProfileError,
    Schedule,
    Slot,
    load,
    plan_for_day,
    save,
)

CANCEL = object()


def _ask(question: str, options, hints: dict[str, str] | None = None):
    print(f"\n{question}")
    for number, option in enumerate(options, start=1):
        hint = f" — {hints[option.code]}" if hints and option.code in hints else ""
        print(f"  {number}. {option.label}{hint}")
    while True:
        raw = input("Номер (пусто — отмена): ").strip()
        if not raw:
            return CANCEL
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1].code
        print("Не понял. Введите номер из списка.")


def _ask_count(default: int = DEFAULT_COUNT) -> int:
    while True:
        raw = input(f"\nСколько присылать из этого блока? [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= MAX_COUNT:
            return int(raw)
        print(f"Нужно число от 1 до {MAX_COUNT}.")


def _ask_topics(kind: str, audience: str) -> tuple[str, ...]:
    topics = topics_for(kind, audience)
    print("\nТемы блока. Они чередуются: каждую неделю — следующая по списку.")
    for number, topic in enumerate(topics, start=1):
        print(f"  {number:2}. {topic.title}")
    raw = input("Номера через запятую (пусто — все): ").strip()
    if not raw:
        return ()
    chosen: list[str] = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit() and 1 <= int(part) <= len(topics):
            slug = topics[int(part) - 1].slug
            if slug not in chosen:
                chosen.append(slug)
    if not chosen:
        print("Ничего не выбрано — беру все темы.")
    return tuple(chosen)


def ask_slot() -> Slot | None:
    kind = _ask("Что присылать в этом блоке?", KINDS, KIND_HINTS)
    if kind is CANCEL:
        return None

    platform = ""
    if kind not in KINDS_WITHOUT_PLATFORM:
        answer = _ask("Платформа?", PLATFORMS)
        if answer is CANCEL:
            return None
        platform = answer
    else:
        print("\nСекции сайта бывают только веб — платформу не спрашиваю.")

    audience = _ask(
        "Аудитория? (у Mobbin нет такого фильтра — разделение задаётся "
        "формулировкой запроса)",
        AUDIENCES,
    )
    if audience is CANCEL:
        return None

    return Slot(
        kind=kind,
        platform=platform,
        audience=audience,
        count=_ask_count(),
        topics=_ask_topics(kind, audience),
    )


def _next_date(weekday: int, since: date) -> date:
    return since + timedelta(days=(weekday - since.weekday()) % 7)


def describe(schedule: Schedule, since: date | None = None) -> str:
    """Неделя целиком: что и когда уходит, с темами ближайшего раза."""
    since = since or date.today()
    lines: list[str] = []
    for index, day in enumerate(schedule.days):
        name = WEEKDAY_NAMES[index]
        if day is None:
            lines.append(f"{name} — письма нет")
            continue
        lines.append(f"{name} — {day.title or 'без названия'}")
        planned = plan_for_day(schedule, _next_date(index, since))
        if planned is None:
            continue
        for slot, topic in planned[1]:
            rotation = len(slot.rotation())
            lines.append(
                f"    {slot.title()} — {slot.count} шт., "
                f"ближайшая тема «{topic.title}» (в ротации {rotation})"
            )
    return "\n".join(lines)


def edit_day(schedule: Schedule, index: int) -> Schedule:
    name = WEEKDAY_NAMES[index]
    print(f"\n=== {name} ===")
    if schedule.days[index] is not None:
        if input("Сделать этот день выходным? [y/N]: ").strip().lower() in ("y", "д", "да"):
            days = list(schedule.days)
            days[index] = None
            return Schedule(days=tuple(days))

    title = input(f"Название дня (например «Геймификация»): ").strip()

    slots: list[Slot] = []
    while True:
        slot = ask_slot()
        if slot is None:
            break
        slots.append(slot)
        print(f"\nБлок добавлен: {slot.title()}")
        if input("Добавить ещё блок в этот день? [y/N]: ").strip().lower() not in ("y", "д", "да"):
            break

    if not slots:
        print(f"{name}: блоков не задано — день оставлен без изменений.")
        return schedule

    days = list(schedule.days)
    days[index] = Day(title=title, slots=tuple(slots))
    return Schedule(days=tuple(days))


def main() -> int:
    config = Config.from_env()
    path = config.profile_path

    try:
        schedule = load(path)
    except ProfileError as exc:
        print(f"Текущее расписание повреждено: {exc}", file=sys.stderr)
        return 1

    while True:
        print("\nСейчас неделя выглядит так:\n")
        print(describe(schedule))
        print("\nКакой день править? Номер 1–7, пусто — закончить.")
        for number, name in enumerate(WEEKDAY_NAMES, start=1):
            print(f"  {number}. {name}")
        raw = input("Номер: ").strip()
        if not raw:
            break
        if not (raw.isdigit() and 1 <= int(raw) <= 7):
            print("Нужен номер от 1 до 7.")
            continue

        candidate = edit_day(schedule, int(raw) - 1)
        try:
            candidate.validate()
        except ProfileError as exc:
            print(f"Так нельзя: {exc}")
            continue
        schedule = candidate

    try:
        save(path, schedule)
    except ProfileError as exc:
        print(f"Не сохранил: {exc}", file=sys.stderr)
        return 1

    print(f"\nГотово, расписание записано в {path}\n")
    print(describe(schedule))
    print("\nПроверить: .venv/bin/python -m inspobot.daily --plan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
