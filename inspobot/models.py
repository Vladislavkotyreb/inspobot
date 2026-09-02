"""Данные подборки. Ни сети, ни SDK — чтобы форматирование и разбор ответа
проверялись тестами без установленных зависимостей.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .topics import Topic


@dataclass(frozen=True)
class Pick:
    platform: str          # "ios" | "web"
    screen_id: str
    mobbin_url: str
    image_url: str
    app_name: str
    pattern: str
    note: str


@dataclass(frozen=True)
class Digest:
    day: date
    mobile_topic: Topic
    desktop_topic: Topic
    summary: str
    picks: tuple[Pick, ...]

    def by_platform(self, platform: str) -> tuple[Pick, ...]:
        return tuple(p for p in self.picks if p.platform == platform)
