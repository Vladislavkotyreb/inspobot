"""Профиль дайджеста: из чего он состоит.

Продукт — утреннее письмо, а не бот на постоянном прогоне. Поэтому выбор
атрибутов делается один раз и сохраняется, а ежедневный запуск просто читает
сохранённое. Никакого процесса между запусками не живёт.

Профиль — набор слотов. Слот отвечает на «что, для кого и на какой платформе»,
а тема внутри слота меняется по ротации: за N дней слот обойдёт весь свой
список и начнёт заново.
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
        """Темы, по которым слот ходит по кругу."""
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
class Profile:
    slots: tuple[Slot, ...]

    def validate(self) -> None:
        if not self.slots:
            raise ProfileError("В профиле нет ни одного слота.")
        kinds = {o.code for o in KINDS}
        platforms = {o.code for o in PLATFORMS}
        audiences = {o.code for o in AUDIENCES}
        for index, slot in enumerate(self.slots, start=1):
            if slot.kind not in kinds:
                raise ProfileError(f"Слот {index}: неизвестный вид «{slot.kind}».")
            if slot.audience not in audiences:
                raise ProfileError(f"Слот {index}: неизвестная аудитория «{slot.audience}».")
            if slot.needs_platform and slot.platform not in platforms:
                raise ProfileError(f"Слот {index}: нужна платформа.")
            if not slot.needs_platform and slot.platform:
                raise ProfileError(f"Слот {index}: у секций сайта платформы нет.")
            if not 1 <= slot.count <= MAX_COUNT:
                raise ProfileError(f"Слот {index}: сколько присылать — от 1 до {MAX_COUNT}.")
            for slug in slot.topics:
                if find_topic(slot.kind, slot.audience, slug) is None:
                    raise ProfileError(f"Слот {index}: темы «{slug}» нет в этом наборе.")


# Мобилка для людей, десктоп для бизнеса и один многошаговый сценарий —
# то, с чего разумно начать, если ничего не настраивать.
DEFAULT_PROFILE = Profile(
    slots=(
        Slot(kind="s", platform="i", audience="c", count=3),
        Slot(kind="s", platform="w", audience="b", count=3),
        Slot(kind="f", platform="i", audience="c", count=2),
    )
)


def plan_for_day(profile: Profile, day: date) -> tuple[tuple[Slot, Topic], ...]:
    """Что именно ищем сегодня: по одной теме на слот.

    Внутри слота повтор темы наступает через len(rotation) дней. Между слотами
    сочетание тем повторяется через НОК длин их списков — специально это не
    разводится, десяти дней достаточно.
    """
    plan: list[tuple[Slot, Topic]] = []
    for index, slot in enumerate(profile.slots):
        rotation = slot.rotation()
        position = (day.toordinal() + index * 3) % len(rotation)
        plan.append((slot, rotation[position]))
    return tuple(plan)


# --- хранение ---------------------------------------------------------------


def load(path: Path) -> Profile:
    """Профиль с диска; если файла нет — умолчание, чтобы бот работал сразу."""
    if not Path(path).exists():
        return DEFAULT_PROFILE
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        slots = tuple(
            Slot(
                kind=str(item["kind"]),
                platform=str(item.get("platform", "")),
                audience=str(item["audience"]),
                count=int(item.get("count", DEFAULT_COUNT)),
                topics=tuple(str(t) for t in item.get("topics", ())),
            )
            for item in raw["slots"]
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ProfileError(f"Файл профиля {path} не читается: {exc}") from exc
    profile = Profile(slots=slots)
    profile.validate()
    return profile


def save(path: Path, profile: Profile) -> None:
    profile.validate()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = {"slots": [asdict(slot) for slot in profile.slots]}
    for slot in payload["slots"]:
        slot["topics"] = list(slot["topics"])
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
