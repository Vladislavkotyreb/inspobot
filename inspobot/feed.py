"""Вторая утренняя рассылка: лента из открытых источников.

    python -m inspobot.feed            # собрать и отправить
    python -m inspobot.feed --probe    # что отвечает каждый источник
    python -m inspobot.feed --list     # состав ленты, без сети
    python -m inspobot.feed --dry-run  # показать в консоли

Ключ Anthropic здесь не нужен и не спрашивается: всё, из чего собрана лента,
источники публикуют машиночитаемо. Прогон стоит трафик.

Пакет `anthropic` не нужен тоже — и это проверяется тестом, а не обещанием.
Ни один импорт отсюда не должен приводить к `daily.py` или `curator.py`:
лента обязана подниматься на машине, где из зависимостей один httpx.

Повторный запуск в тот же день ничего не отправит без --force, так что
«дёрнуть ещё раз после сбоя» безопасно.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from . import chats as chats_file
from .config import Config
from .fetcher import SHOT_TEMPLATE, Fetcher
from .gather import Trace, collect
from .harvest import link_key
from .logs import setup as setup_logging
from .models import Block, Feed, Find
from .render import feed_header_html, find_caption_html, find_keyboard, find_text_html
from .sources import Source, SourcesError, by_section, enabled, studios_file
from .sources import load as load_sources
from .state import SentLink, Store
from .telegram import Telegram, TelegramError

log = logging.getLogger("inspobot.feed")


def today_in(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


# --- отправка ---------------------------------------------------------------


async def send_find(
    telegram: Telegram,
    block: Block,
    find: Find,
    index: int,
    total: int,
    chat_id: str | None,
    as_document: bool,
) -> bool:
    """Одна находка: картинка с подписью и кнопкой, либо текст со ссылкой.

    Картинки у ленты — чужие: обложка кейса, снимок сайта, превью шота.
    Любая из них может не отдаться, и тогда находку всё равно надо показать,
    просто текстом. Молча пропускать нельзя: именно так раздел из четырёх
    кейсов превращается в раздел из одного, и это не видно ни в логе, ни в
    письме.
    """
    caption = find_caption_html(block, find, index, total)
    keyboard = find_keyboard(find)
    await telegram.pause()
    if find.image:
        mid = await telegram.send_media(
            find.image, caption, as_document=as_document, keyboard=keyboard, chat_id=chat_id
        )
        if mid is not None:
            return True
        log.info("Картинка %s не ушла — отправляю текстом", find.image)
        await telegram.pause()
    try:
        await telegram.send_message(
            find_text_html(block, find, index, total), chat_id=chat_id, keyboard=keyboard
        )
    except TelegramError as exc:
        log.warning("Находка %s не ушла: %s", find.url, exc)
        return False
    return True


async def deliver(
    telegram: Telegram,
    feed: Feed,
    chat_id: str | None,
    as_document: bool,
    remembered: int = 0,
) -> int:
    await telegram.send_message(feed_header_html(feed, remembered), chat_id=chat_id)
    sent = 0
    for block in feed.blocks:
        total = len(block.finds)
        for index, find in enumerate(block.finds, start=1):
            if await send_find(telegram, block, find, index, total, chat_id, as_document):
                sent += 1
    return sent


async def broadcast(
    telegram: Telegram,
    feed: Feed,
    targets: Sequence[str],
    as_document: bool,
    remembered: int = 0,
) -> tuple[int, list[str]]:
    """Одна лента во все чаты. Упавший чат не роняет рассылку."""
    sent = 0
    failed: list[str] = []
    for target in targets:
        try:
            sent += await deliver(telegram, feed, target, as_document, remembered)
        except TelegramError as exc:
            log.warning("Чат %s не получил ленту: %s", target, exc)
            failed.append(target)
    return sent, failed


# --- сборка -----------------------------------------------------------------


def sources_for(config: Config, only: Sequence[str] = ()) -> tuple[Source, ...]:
    found = enabled(load_sources(config.sources_path, config.studios_path))
    if only:
        wanted = set(only)
        found = tuple(s for s in found if s.section in wanted or s.key in wanted)
        if not found:
            raise SourcesError(f"Под «{', '.join(sorted(wanted))}» не подошёл ни один источник")
    return found


def pullers_for(config: Config, sources: Sequence[Source], day: date) -> dict:
    """Сборщики, которые ходят не по HTTP.

    Сейчас такой один — Mobbin по MCP. Подключается, только если источник
    включён: без него не нужен ни токен, ни импорт, и лента поднимается на
    машине, где вход в Mobbin никто не проходил.
    """
    if not any("mobbin" in s.ways for s in sources):
        return {}
    from . import mobbin_feed

    return {
        "mobbin": mobbin_feed.puller(
            config.mobbin_mcp_url,
            config.mobbin_token_file,
            config.mobbin_access_token,
            day,
        )
    }


async def build(
    config: Config,
    store: Store | None,
    day: date,
    *,
    only: Sequence[str] = (),
    save_to: Path | None = None,
) -> tuple[Feed, tuple[Trace, ...]]:
    sources = sources_for(config, only)
    seen = store.known_links() if store is not None else set()
    log.info("Источников %d, в памяти адресов %d", len(sources), len(seen))
    async with Fetcher(
        respect_robots=config.feed_robots, dump_to=save_to
    ) as fetcher:
        return await collect(
            sources,
            fetcher,
            day,
            seen=seen,
            shots=config.feed_shots,
            shot_template=config.feed_shot_url or SHOT_TEMPLATE,
            pullers=pullers_for(config, sources, day),
        )


def remember(store: Store, feed: Feed) -> int:
    return store.mark_links(
        [
            SentLink(link_key(f.url), f.source, f.url, f.title)
            for f in feed.finds
        ],
        feed.day,
    )


# --- команды ----------------------------------------------------------------


def show_sources(config: Config) -> int:
    """Состав ленты без единого запроса — проверка, что файлы читаются."""
    try:
        sources = load_sources(config.sources_path, config.studios_path)
    except SourcesError as exc:
        print(f"Источники не читаются: {exc}", file=sys.stderr)
        return 1
    chosen = studios_file(config.studios_path)
    print(f"Правки:  {config.sources_path}" + ("" if config.sources_path.exists() else " (нет)"))
    print(f"Студии:  {chosen or 'встроенный список в sources.py'}")
    print()
    for spec, group in by_section(sources):
        print(f"{spec.icon} {spec.title} — до {spec.limit}")
        for source in group:
            ways = "/".join(source.ways)
            print(f"    {source.key:<26} {ways:<12} {source.url}")
            if source.note:
                print(f"    {'':<26} {'':<12} {source.note}")
        print()
    off = [s.key for s in sources if not s.enabled]
    if off:
        print("Выключены: " + ", ".join(off))
    return 0


async def probe(config: Config, day: date, only: Sequence[str], save_to: Path | None) -> int:
    """Что отвечает каждый источник — построчно, без отправки.

    Это главная команда отладки. Разбор чинится только так: увидеть, что
    сайт вернул на самом деле, а не гадать, почему раздел пуст.
    """
    feed, traces = await build(config, None, day, only=only, save_to=save_to)
    width = max((len(t.source) for t in traces), default=10)
    for trace in traces:
        print(trace.line().replace(f"{trace.source:<24}", f"{trace.source:<{width + 2}}"))
    print()
    ok = {t.source for t in traces if not t.error and t.kept}
    dead = sorted({t.source for t in traces} - ok)
    print(f"Ответили находками: {len(ok)}")
    if dead:
        print("Промолчали: " + ", ".join(dead))
    print(f"Разделов в письме: {len(feed.blocks)}, находок: {len(feed.finds)}")
    if save_to:
        print(f"Страницы сохранены в {save_to}")
    # Пустая лента — это не успех прогона, и код возврата должен это говорить:
    # cron иначе будет считать, что всё хорошо, ровно до первой жалобы.
    return 0 if feed.finds else 1


async def run_once(
    config: Config,
    *,
    day: date | None = None,
    force: bool = False,
    dry_run: bool = False,
    chat_id: str | None = None,
    only: Sequence[str] = (),
) -> int:
    store = Store(config.db_path)
    day = day or today_in(config.timezone)

    if store.feed_sent_today(day) and not force:
        log.info("Лента на %s уже уходила — пропускаю", day.isoformat())
        return 0

    run_id = store.start_feed_run(day)
    try:
        feed, _ = await build(config, store, day, only=only)
    except Exception as exc:  # noqa: BLE001 — любой сбой должен попасть в лог
        store.finish_feed_run(run_id, ok=False, error=repr(exc))
        log.exception("Лента не собралась")
        raise

    if not feed.finds:
        store.finish_feed_run(run_id, ok=False, error="источники ничего не вернули")
        log.warning("Ни один источник не дал находок — письма не будет")
        return 0

    if dry_run:
        print(feed_header_html(feed))
        for block in feed.blocks:
            for index, find in enumerate(block.finds, start=1):
                print("---")
                print(find_caption_html(block, find, index, len(block.finds)))
                print(find.url)
                if find.image:
                    print(find.image)
        store.finish_feed_run(run_id, ok=False, finds=len(feed.finds), error="dry-run")
        return len(feed.finds)

    targets = chats_file.load(config.chats_path, config.telegram_chat_id) if not chat_id else (chat_id,)
    if not targets:
        store.finish_feed_run(run_id, ok=False, error="некому отправлять")
        raise TelegramError(
            f"Список получателей пуст: заполните TELEGRAM_CHAT_ID или {config.chats_path}"
        )

    # Память пишется до отправки: повтор после половины доставленной ленты
    # хуже, чем пропуск одной находки.
    fresh = remember(store, feed)
    async with Telegram(config.telegram_token, config.telegram_chat_id) as tg:
        sent, failed = await broadcast(
            tg, feed, targets, config.feed_image_mode == "document", fresh
        )
    if len(failed) == len(targets):
        error = f"ни один из {len(targets)} чатов не принял ленту"
        store.finish_feed_run(run_id, ok=False, error=error)
        raise TelegramError(error)
    if failed:
        log.warning("Не доставлено в %d из %d чатов: %s", len(failed), len(targets), failed)

    store.finish_feed_run(run_id, ok=True, finds=sent)
    log.info("Отправлено находок: %d (новых адресов: %d)", sent, fresh)
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Лента из открытых источников в Telegram")
    parser.add_argument("--force", action="store_true", help="отправить, даже если сегодня уже слали")
    parser.add_argument("--dry-run", action="store_true", help="показать в консоли, ничего не отправлять")
    parser.add_argument("--probe", action="store_true", help="что отвечает каждый источник")
    parser.add_argument("--list", action="store_true", help="состав ленты, без обращений к сети")
    parser.add_argument("--save", metavar="КАТАЛОГ", help="сохранить скачанные страницы (для --probe)")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="КЛЮЧ",
        help="только этот раздел или источник; можно повторять",
    )
    parser.add_argument("--day", help="дата в формате ГГГГ-ММ-ДД (по умолчанию сегодня)")
    parser.add_argument("--chat", help="отправить в один конкретный чат")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    config = Config.from_env()
    day = date.fromisoformat(args.day) if args.day else today_in(config.timezone)

    if args.list:
        return show_sources(config)

    try:
        if args.probe:
            return asyncio.run(
                probe(config, day, args.only, Path(args.save) if args.save else None)
            )
        if not args.dry_run:
            config.require("telegram_token", "telegram_chat_id")
        asyncio.run(
            run_once(
                config,
                day=day,
                force=args.force or bool(args.day),
                dry_run=args.dry_run,
                chat_id=args.chat,
                only=args.only,
            )
        )
    except SourcesError as exc:
        print(f"Источники: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — cron читает только код возврата
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        print(f"Ошибка: {detail}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
