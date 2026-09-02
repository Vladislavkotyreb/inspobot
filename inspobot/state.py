"""Состояние бота в SQLite: что уже присылали и когда.

Нужно для двух вещей: не повторять один и тот же экран (Mobbin умеет исключать
по id) и не слать вторую подборку, если cron дёрнул задачу дважды.
"""

from __future__ import annotations

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
"""


@dataclass(frozen=True)
class SeenScreen:
    screen_id: str
    platform: str
    app_name: str
    mobbin_url: str


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
