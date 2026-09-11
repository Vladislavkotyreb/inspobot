"""Данные дайджеста. Ни сети, ни SDK — чтобы форматирование и разбор ответа
проверялись тестами без установленных зависимостей.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .catalog import Topic
from .profile import Slot


@dataclass(frozen=True)
class Pick:
    platform: str          # "ios" | "web"
    screen_id: str
    mobbin_url: str
    image_url: str
    app_name: str
    pattern: str
    note: str
    # Шаги сценария для флоу: превью каждого экрана по порядку. У отдельных
    # экранов и секций пусто — там показывать нечего, кроме самой картинки.
    screens: tuple[str, ...] = ()
    # Насколько находка примечательна, 1–10. Ставит модель в момент отбора,
    # внутри того же запроса — отдельного вызова это не стоит. По этой оценке
    # потом собирается «топ за неделю» без единого обращения к API.
    score: int = 0

    @property
    def images(self) -> tuple[str, ...]:
        """Всё, что нужно показать: у флоу — все шаги, у остального — одна."""
        return self.screens or (self.image_url,)


@dataclass(frozen=True)
class Section:
    """Один блок дайджеста: слот профиля, тема дня и что нашлось."""

    slot: Slot
    topic: Topic
    picks: tuple[Pick, ...]

    def title(self) -> str:
        return f"{self.slot.title()} · {self.topic.title}"


@dataclass(frozen=True)
class Digest:
    day: date
    title: str
    summary: str
    sections: tuple[Section, ...]

    @property
    def picks(self) -> tuple[Pick, ...]:
        return tuple(pick for section in self.sections for pick in section.picks)

    @property
    def screen_picks(self) -> tuple[Pick, ...]:
        """Только экраны — их id уходят в дедупликацию, флоу и секции нет."""
        return tuple(
            pick
            for section in self.sections
            if section.slot.kind == "s"
            for pick in section.picks
        )
