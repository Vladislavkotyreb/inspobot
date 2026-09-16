"""Сетевой слой ленты: скачать адрес и вернуть текст. Больше ничего.

Разбор живёт в `harvest.py` и про сеть не знает — поэтому проверяется
тестами на сохранённых страницах. Здесь наоборот: логики разбора нет,
зато есть всё, из-за чего утренний прогон падает в реальности — таймауты,
редиректы, Cloudflare, robots.txt и слишком бодрый параллелизм.

Ответ никогда не поднимает исключение наружу: сбой — это `Page` с
заполненным `error`. Один умерший сайт не должен отменять письмо, а должен
попасть строкой в отчёт `probe`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .sources import USER_AGENT

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Больше четырёх одновременных запросов не нужно: источников два десятка,
# а выглядеть это должно как человек с браузером, а не как краулер.
PARALLEL = 4
# Пауза между запросами к одному хосту. У Dribbble на страницу приходится
# до шести дочитываний og-тегов подряд — без паузы это ровно тот профиль
# нагрузки, на который отвечают 429.
HOST_PAUSE = 0.7
MAX_BYTES = 4 * 1024 * 1024

# Скриншотилка для находок, у которых нет ни своей картинки, ни og:image.
# WordPress отдаёт снимок по адресу страницы, без ключа и регистрации.
# Первый запрос часто возвращает заглушку «снимаем» — Telegram её тоже
# покажет, поэтому дешевле подождать один прогон, чем городить опрос.
SHOT_TEMPLATE = "https://s.wordpress.com/mshots/v1/{url}?w=1200&h=900"


def shot_url(page_url: str, template: str = SHOT_TEMPLATE) -> str:
    if not page_url or not template:
        return ""
    return template.replace("{url}", quote(page_url, safe=""))


@dataclass(frozen=True)
class Page:
    url: str
    status: int = 0
    body: str = ""
    content_type: str = ""
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300 and bool(self.body)

    def why(self) -> str:
        """Одна строка для отчёта: почему страница не годится."""
        if self.error:
            return self.error
        if not 200 <= self.status < 300:
            return f"HTTP {self.status}"
        if not self.body:
            return "пустой ответ"
        return ""


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


@dataclass
class Fetcher:
    """Клиент на один прогон: общий пул соединений, общий кеш robots.txt.

    Создаётся через `async with`, иначе httpx оставит сокеты открытыми и
    прогон под cron не завершится.
    """

    timeout: httpx.Timeout = field(default_factory=lambda: TIMEOUT)
    parallel: int = PARALLEL
    respect_robots: bool = True
    user_agent: str = USER_AGENT
    dump_to: Path | None = None
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _gate: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict, init=False, repr=False)
    _last: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )
        self._gate = asyncio.Semaphore(self.parallel)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- вежливость --------------------------------------------------------

    def _lock(self, host: str) -> asyncio.Lock:
        lock = self._locks.get(host)
        if lock is None:
            lock = self._locks[host] = asyncio.Lock()
        return lock

    async def _wait_turn(self, host: str) -> None:
        """Пауза между запросами к одному хосту, к разным — не мешаем.

        Замок именно на хост: без него две задачи проверяют «прошло ли
        HOST_PAUSE» одновременно, обе видят «прошло» и стучатся вместе.
        """
        async with self._lock(host):
            gap = time.monotonic() - self._last.get(host, 0.0)
            if gap < HOST_PAUSE:
                await asyncio.sleep(HOST_PAUSE - gap)
            self._last[host] = time.monotonic()

    async def allowed(self, url: str) -> bool:
        """Что говорит robots.txt про этот адрес.

        Нет файла, не отдался, не разобрался — считаем «можно»: отсутствие
        запрета это и есть разрешение, а падать из-за недоступного
        robots.txt значит отменить письмо из-за чужой опечатки в nginx.
        """
        if not self.respect_robots:
            return True
        host = host_of(url)
        if host not in self._robots:
            self._robots[host] = await self._load_robots(url)
        parser = self._robots[host]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    async def _load_robots(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        page = await self._raw(f"{parts.scheme}://{parts.netloc}/robots.txt", {}, robots=False)
        if not page.ok:
            return None
        parser = RobotFileParser()
        try:
            parser.parse(page.body.splitlines())
        except Exception:  # noqa: BLE001 — кривой robots.txt не повод падать
            return None
        return parser

    # --- запрос ------------------------------------------------------------

    async def get(self, url: str, headers: Mapping[str, str] | None = None) -> Page:
        if not await self.allowed(url):
            return Page(url=url, error="запрещено robots.txt")
        return await self._raw(url, headers or {})

    async def _raw(self, url: str, headers: Mapping[str, str], robots: bool = True) -> Page:
        if self._client is None or self._gate is None:
            raise RuntimeError("Fetcher используется вне `async with`")
        started = time.monotonic()
        async with self._gate:
            await self._wait_turn(host_of(url))
            try:
                response = await self._client.get(url, headers=dict(headers))
            except httpx.HTTPError as exc:
                # У таймаутов httpx текст пустой, и наружу уходило «Ошибка:»
                # без единого слова — поэтому тип пишем всегда.
                detail = f"{type(exc).__name__}" + (f": {exc}" if str(exc) else "")
                log.info("%s — не скачалось (%s)", url, detail)
                return Page(url=url, error=detail, elapsed=time.monotonic() - started)

        body = response.text[:MAX_BYTES] if response.content else ""
        page = Page(
            url=str(response.url),
            status=response.status_code,
            body=body,
            content_type=response.headers.get("content-type", ""),
            elapsed=time.monotonic() - started,
        )
        self._dump(page)
        return page

    def _dump(self, page: Page) -> None:
        """Сохранить ответ на диск — это `probe --save`.

        Чинить разбор по живому сайту невозможно: страница меняется между
        двумя запусками, и непонятно, правило поправилось или сайт. По
        сохранённому файлу правило правится за минуту и закрепляется тестом.
        """
        if self.dump_to is None or not page.body:
            return
        name = "".join(c if c.isalnum() or c in "-._" else "_" for c in page.url)[:120]
        try:
            self.dump_to.mkdir(parents=True, exist_ok=True)
            (self.dump_to / f"{name}.txt").write_text(page.body, encoding="utf-8")
        except OSError as exc:
            log.warning("Не записал %s: %s", name, exc)
