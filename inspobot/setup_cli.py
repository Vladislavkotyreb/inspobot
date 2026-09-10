"""Пошаговая настройка дайджеста: `python -m inspobot.setup_cli`.

Проходится один раз, в терминале. Результат — профиль на диске, по которому
ежедневный запуск собирает письмо. Никакого процесса между запусками не живёт:
продукт — дайджест, а не бот на постоянном прогоне.
"""

from __future__ import annotations

import sys
from typing import Sequence

from .catalog import AUDIENCES, KIND_HINTS, KINDS, KINDS_WITHOUT_PLATFORM, PLATFORMS, Option
from .catalog import topics_for
from .config import Config
from .profile import DEFAULT_COUNT, MAX_COUNT, Profile, ProfileError, Slot, load, save

CANCEL = object()


def _ask(question: str, options: Sequence[Option], hints: dict[str, str] | None = None):
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
    print("\nТемы блока. Они чередуются: каждый день — следующая по списку.")
    for number, topic in enumerate(topics, start=1):
        print(f"  {number}. {topic.title}")
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


def describe(profile: Profile) -> str:
    lines = []
    for index, slot in enumerate(profile.slots, start=1):
        rotation = slot.rotation()
        names = ", ".join(t.title for t in rotation)
        lines.append(f"{index}. {slot.title()} — {slot.count} шт.")
        lines.append(f"   Темы ({len(rotation)}, по очереди): {names}")
    return "\n".join(lines)


def main() -> int:
    config = Config.from_env()
    path = config.profile_path

    try:
        current = load(path)
    except ProfileError as exc:
        print(f"Текущий профиль повреждён: {exc}", file=sys.stderr)
        current = None

    if current:
        print("Сейчас дайджест собирается так:\n")
        print(describe(current))

    print(
        "\nСоберём профиль заново. Блок — это одна строка дайджеста:"
        "\nчто ищем, для кого, на какой платформе."
    )

    slots: list[Slot] = []
    while True:
        slot = ask_slot()
        if slot is None:
            break
        slots.append(slot)
        print(f"\nБлок добавлен: {slot.title()}")
        if input("Добавить ещё блок? [y/N]: ").strip().lower() not in ("y", "д", "да"):
            break

    if not slots:
        print("\nНичего не выбрано — профиль не изменился.")
        return 0

    profile = Profile(slots=tuple(slots))
    try:
        save(path, profile)
    except ProfileError as exc:
        print(f"Не сохранил: {exc}", file=sys.stderr)
        return 1

    print(f"\nГотово, профиль записан в {path}\n")
    print(describe(profile))
    print("\nПроверить, что получится: .venv/bin/python -m inspobot.daily --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
