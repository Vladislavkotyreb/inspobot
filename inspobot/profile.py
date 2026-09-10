"""Расписание дайджеста: что уходит в какой день недели.

Продукт — утреннее письмо, а не бот на постоянном прогоне, поэтому состав
задаётся один раз и лежит на диске. У каждого дня недели свой набор блоков:
понедельник про геймификацию, вторник про сложные процессы и так далее.
День можно оставить пустым — тогда письма в этот день не будет.

Блок отвечает на «что, для кого и на какой платформе», а тема внутри блока
меняется от недели к неделе.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .catalog import (
    AUDIENCES,
    KINDS,
    KINDS_WITHOUT_PLATFORM,
    PLATFORM_API,
    PLATFORMS,
    Topic,
    find_topic,
    label,
    topics_for,
)

DEFAULT_COUNT = 3
MAX_COUNT = 8

WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = (
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
)


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Slot:
    kind: str                      # s | f | w
    platform: str                  # i | w | "" для секций
    audience: str                  # b | c
    count: int = DEFAULT_COUNT
    topics: tuple[str, ...] = ()   # пусто — весь список набора

    @property
    def needs_platform(self) -> bool:
        return self.kind not in KINDS_WITHOUT_PLATFORM

    @property
    def api_platform(self) -> str:
        return PLATFORM_API[self.platform] if self.needs_platform else "web"

    def rotation(self) -> tuple[Topic, ...]:
        """Темы, по которым блок ходит по кругу."""
        available = topics_for(self.kind, self.audience)
        if not self.topics:
            return available
        chosen = [t for t in available if t.slug in self.topics]
        return tuple(chosen) or available

    def title(self) -> str:
        parts = [label(KINDS, self.kind)]
        if self.needs_platform:
            parts.append(label(PLATFORMS, self.platform))
        parts.append(label(AUDIENCES, self.audience))
        return " · ".join(parts)


@dataclass(frozen=True)
class Day:
    """Один день недели: заголовок письма и его блоки."""

    title: str
    slots: tuple[Slot, ...]


@dataclass(frozen=True)
class Schedule:
    days: tuple[Day | None, ...]   # ровно 7, индекс = date.weekday()

    def for_day(self, day: date) -> Day | None:
        return self.days[day.weekday()]

    def validate(self) -> None:
        if len(self.days) != 7:
            raise ProfileError("В расписании должно быть ровно семь дней.")
        if all(day is None for day in self.days):
            raise ProfileError("Все дни пустые — дайджест никогда не уйдёт.")
        kinds = {o.code for o in KINDS}
        platforms = {o.code for o in PLATFORMS}
        audiences = {o.code for o in AUDIENCES}
        for index, day in enumerate(self.days):
            if day is None:
                continue
            where = WEEKDAY_NAMES[index]
            if not day.slots:
                raise ProfileError(f"{where}: день не пустой, но блоков нет.")
            for number, slot in enumerate(day.slots, start=1):
                prefix = f"{where}, блок {number}"
                if slot.kind not in kinds:
                    raise ProfileError(f"{prefix}: неизвестный вид «{slot.kind}».")
                if slot.audience not in audiences:
                    raise ProfileError(f"{prefix}: неизвестная аудитория «{slot.audience}».")
                if slot.needs_platform and slot.platform not in platforms:
                    raise ProfileError(f"{prefix}: нужна платформа.")
                if not slot.needs_platform and slot.platform:
                    raise ProfileError(f"{prefix}: у секций сайта платформы нет.")
                if not 1 <= slot.count <= MAX_COUNT:
                    raise ProfileError(f"{prefix}: количество — от 1 до {MAX_COUNT}.")
                for slug in slot.topics:
                    if find_topic(slot.kind, slot.audience, slug) is None:
                        raise ProfileError(f"{prefix}: темы «{slug}» нет в этом наборе.")


# --- неделя по умолчанию ----------------------------------------------------

MONDAY = Day(
    title="Геймификация",
    slots=(
        Slot("s", "i", "c", count=4,
             topics=("progress", "streak", "rewards", "challenges", "leaderboard", "levels")),
        Slot("f", "i", "c", count=1, topics=("gamified-onboarding",)),
    ),
)

TUESDAY = Day(
    title="Логика сложных процессов",
    slots=(
        Slot("f", "w", "b", count=3,
             topics=("setup", "integration", "import", "approval", "automation", "migration")),
        Slot("s", "w", "b", count=2,
             topics=("workflow", "automation", "approval", "permissions-matrix", "filters")),
    ),
)

WEDNESDAY = Day(
    title="Пейволлы и оплата",
    slots=(
        Slot("s", "i", "c", count=3,
             topics=("paywall", "checkout", "promo", "subscription", "trial")),
        Slot("f", "i", "c", count=2,
             topics=("subscribe", "purchase", "addcard", "upgrade-c", "cancel")),
    ),
)

THURSDAY = Day(
    title="Плотные данные: таблицы и дашборды",
    slots=(
        Slot("s", "w", "b", count=4,
             topics=("dashboard", "table", "filters", "audit", "inbox")),
    ),
)

FRIDAY = Day(
    title="Как продукты себя продают",
    slots=(
        Slot("w", "", "b", count=3),
        Slot("w", "", "c", count=2),
    ),
)

SUNDAY = Day(
    title="Первое впечатление",
    slots=(
        Slot("f", "i", "c", count=2, topics=("signup", "onboarding", "kyc")),
        Slot("s", "i", "c", count=2, topics=("onboarding", "empty", "permissions")),
    ),
)

# Суббота пустая: один день без письма в неделю — это не пробел, а пауза.
DEFAULT_SCHEDULE = Schedule(
    days=(MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, None, SUNDAY)
)


# --- план на конкретную дату ------------------------------------------------


def plan_for_day(
    schedule: Schedule, day: date
) -> tuple[Day, tuple[tuple[Slot, Topic], ...]] | None:
    """День недели и его темы на эту дату. None — в этот день письма нет.

    Ротация считается по номеру недели, а не по дню: один и тот же день недели
    приходит раз в семь суток, и отсчёт по дням застревал бы на месте, когда
    длина списка тем кратна семи.
    """
    day_plan = schedule.for_day(day)
    if day_plan is None:
        return None
    week = day.toordinal() // 7
    plan: list[tuple[Slot, Topic]] = []
    for index, slot in enumerate(day_plan.slots):
        rotation = slot.rotation()
        plan.append((slot, rotation[(week + index * 3) % len(rotation)]))
    return day_plan, tuple(plan)


# --- хранение ---------------------------------------------------------------


def _slot_from(item: dict) -> Slot:
    return Slot(
        kind=str(item["kind"]),
        platform=str(item.get("platform", "")),
        audience=str(item["audience"]),
        count=int(item.get("count", DEFAULT_COUNT)),
        topics=tuple(str(t) for t in item.get("topics", ())),
    )


def load(path: Path) -> Schedule:
    """Расписание с диска; если файла нет — умолчание, чтобы работало сразу."""
    if not Path(path).exists():
        return DEFAULT_SCHEDULE
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if "days" not in raw and "slots" in raw:
            # Старый формат: один набор блоков на все дни недели.
            day = Day(title="", slots=tuple(_slot_from(i) for i in raw["slots"]))
            return Schedule(days=tuple([day] * 7))
        days: list[Day | None] = []
        for code in WEEKDAY_CODES:
            item = raw["days"].get(code)
            if not item:
                days.append(None)
                continue
            days.append(
                Day(
                    title=str(item.get("title", "")),
                    slots=tuple(_slot_from(s) for s in item["slots"]),
                )
            )
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ProfileError(f"Файл расписания {path} не читается: {exc}") from exc
    schedule = Schedule(days=tuple(days))
    schedule.validate()
    return schedule


def save(path: Path, schedule: Schedule) -> None:
    schedule.validate()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    days: dict[str, dict | None] = {}
    for code, day in zip(WEEKDAY_CODES, schedule.days):
        if day is None:
            days[code] = None
            continue
        slots = []
        for slot in day.slots:
            item = asdict(slot)
            item["topics"] = list(item["topics"])
            slots.append(item)
        days[code] = {"title": day.title, "slots": slots}
    Path(path).write_text(
        json.dumps({"days": days}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
