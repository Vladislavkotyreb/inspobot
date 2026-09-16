"""Данные дайджеста. Ни сети, ни SDK — чтобы форматирование и разбор ответа
проверялись тестами без установленных зависимостей.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .catalog import Topic
from .profile import Slot
from .sources import SectionSpec


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


# --- лента из открытых источников -------------------------------------------
# Второй дайджест бота: сайты дня, кейсы студий, Behance, Dribbble, DProfile,
# Made on Tilda, новые приложения. Собирается без единого обращения к модели —
# всё это источники и так публикуют машиночитаемо, — поэтому и данные здесь
# другие: ни оценки, ни разбора, зато автор, лайки и дата.


@dataclass(frozen=True)
class Find:
    """Находка ленты. `origin` — подпись источника в карточке: раздел один
    («Dribbble»), а источников в нём три, и без подписи непонятно, из какой
    категории шот."""

    source: str
    origin: str
    url: str
    title: str
    author: str = ""
    image: str = ""
    summary: str = ""
    published: date | None = None
    likes: int = 0
    tags: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.title or self.author or self.url


@dataclass(frozen=True)
class Block:
    spec: SectionSpec
    finds: tuple[Find, ...]

    def title(self) -> str:
        return self.spec.title


@dataclass(frozen=True)
class Feed:
    day: date
    blocks: tuple[Block, ...]

    @property
    def finds(self) -> tuple[Find, ...]:
        return tuple(find for block in self.blocks for find in block.finds)
