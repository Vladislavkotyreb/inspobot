"""Состояние бота в SQLite: что уже присылали, когда и куда.

Три задачи. Не повторять один и тот же экран (Mobbin умеет исключать по id).
Не слать вторую подборку, если cron дёрнул задачу дважды. И помнить каждую
находку с её оценкой и номерами сообщений в чатах — из этого собирается
«топ за неделю» без единого обращения к API: Telegram копирует уже
отправленные сообщения, картинки не перекачиваются.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    screen_id  TEXT PRIMARY KEY,
    platform   TEXT NOT NULL,
    app_name   TEXT,
    mobbin_url TEXT,
    sent_on    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_platform_sent ON seen(platform, sent_on DESC);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 0,
    picks      INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS runs_day ON runs(day);

CREATE TABLE IF NOT EXISTS picks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    platform   TEXT NOT NULL,
    screen_id  TEXT NOT NULL,
    app_name   TEXT,
    pattern    TEXT,
    note       TEXT,
    mobbin_url TEXT NOT NULL,
    topic      TEXT,
    block      TEXT,
    score      INTEGER NOT NULL DEFAULT 5,
    UNIQUE(day, screen_id)
);
CREATE INDEX IF NOT EXISTS picks_day_score ON picks(day DESC, score DESC);

CREATE TABLE IF NOT EXISTS deliveries (
    pick_id     INTEGER NOT NULL REFERENCES picks(id),
    chat_id     TEXT NOT NULL,
    message_ids TEXT NOT NULL,
    PRIMARY KEY (pick_id, chat_id)
);
"""


@dataclass(frozen=True)
class SeenScreen:
    screen_id: str
    platform: str
    app_name: str
    mobbin_url: str


@dataclass(frozen=True)
class StoredPick:
    """Находка, как она лежит в базе: то, что нужно для топа."""

    id: int
    day: date
    kind: str
    platform: str
    screen_id: str
    app_name: str
    pattern: str
    note: str
    mobbin_url: str
    topic: str
    block: str
    score: int


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # --- дедупликация ------------------------------------------------------

    def recent_seen_ids(self, platform: str, limit: int = 100) -> list[str]:
        """Последние показанные id по площадке.

        Mobbin принимает не больше 100 значений в exclude_screen_ids, поэтому
        память намеренно скользящая: совсем старые экраны через полгода можно
        показать снова.
        """
        limit = max(0, min(limit, 100))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT screen_id FROM seen WHERE platform = ? "
                "ORDER BY sent_on DESC, rowid DESC LIMIT ?",
                (platform, limit),
            ).fetchall()
        return [r["screen_id"] for r in rows]

    def known_ids(self, screen_ids: Iterable[str]) -> set[str]:
        ids = [i for i in screen_ids if i]
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT screen_id FROM seen WHERE screen_id IN ({placeholders})", ids
            ).fetchall()
        return {r["screen_id"] for r in rows}

    def mark_seen(self, screens: Sequence[SeenScreen], day: date) -> None:
        if not screens:
            return
        with closing(self._connect()) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO seen(screen_id, platform, app_name, mobbin_url, sent_on) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (s.screen_id, s.platform, s.app_name, s.mobbin_url, day.isoformat())
                    for s in screens
                ],
            )
            conn.commit()

    # --- запуски -----------------------------------------------------------

    def sent_today(self, day: date) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE day = ? AND ok = 1 LIMIT 1", (day.isoformat(),)
            ).fetchone()
        return row is not None

    def start_run(self, day: date) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO runs(day, started_at) VALUES (?, ?)",
                (day.isoformat(), datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, ok: bool, picks: int = 0, error: str = "") -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE runs SET ok = ?, picks = ?, error = ? WHERE id = ?",
                (1 if ok else 0, picks, error[:2000], run_id),
            )
            conn.commit()

    # --- находки и доставки --------------------------------------------------

    def record_pick(
        self,
        *,
        day: date,
        kind: str,
        platform: str,
        screen_id: str,
        app_name: str,
        pattern: str,
        note: str,
        mobbin_url: str,
        topic: str,
        block: str,
        score: int,
    ) -> int:
        """Запомнить находку; повтор в тот же день возвращает прежний id."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO picks(day, kind, platform, screen_id, app_name, "
                "pattern, note, mobbin_url, topic, block, score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day.isoformat(), kind, platform, screen_id, app_name, pattern, note,
                 mobbin_url, topic, block, score),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM picks WHERE day = ? AND screen_id = ?",
                (day.isoformat(), screen_id),
            ).fetchone()
        return int(row["id"])

    def record_delivery(self, pick_id: int, chat_id: str, message_ids: Sequence[int]) -> None:
        if not message_ids:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO deliveries(pick_id, chat_id, message_ids) "
                "VALUES (?, ?, ?)",
                (pick_id, chat_id, json.dumps([int(m) for m in message_ids])),
            )
            conn.commit()

    def delivery(self, pick_id: int, chat_id: str) -> list[int]:
        """Номера сообщений, которыми находка ушла в этот чат. Пусто — не уходила."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT message_ids FROM deliveries WHERE pick_id = ? AND chat_id = ?",
                (pick_id, chat_id),
            ).fetchone()
        if row is None:
            return []
        try:
            return [int(m) for m in json.loads(row["message_ids"])]
        except (ValueError, TypeError):
            return []

    def top(self, since: date, until: date, limit: int = 10) -> list[StoredPick]:
        """Лучшее за период: по оценке, при равной — более свежее.

        Одно приложение — один раз: топ из пяти экранов Duolingo никому не
        интересен, даже если все они на девятку.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM picks WHERE day >= ? AND day <= ? "
                "ORDER BY score DESC, day DESC, id DESC",
                (since.isoformat(), until.isoformat()),
            ).fetchall()
        chosen: list[StoredPick] = []
        apps: set[str] = set()
        for row in rows:
            app = (row["app_name"] or "").strip().lower()
            if app and app in apps:
                continue
            apps.add(app)
            chosen.append(
                StoredPick(
                    id=int(row["id"]),
                    day=date.fromisoformat(row["day"]),
                    kind=row["kind"],
                    platform=row["platform"],
                    screen_id=row["screen_id"],
                    app_name=row["app_name"] or "—",
                    pattern=row["pattern"] or "",
                    note=row["note"] or "",
                    mobbin_url=row["mobbin_url"],
                    topic=row["topic"] or "",
                    block=row["block"] or "",
                    score=int(row["score"]),
                )
            )
            if len(chosen) >= limit:
                break
        return chosen
