"""Одна утренняя подборка: собрать и отправить.

Точка входа для системного cron:

    python -m inspobot.daily

Состав дайджеста берётся из профиля (см. `python -m inspobot.setup_cli`).
Повторный запуск в тот же день ничего не отправит — если не передать --force,
поэтому «дёрнуть ещё раз после сбоя» безопасно.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .catalog import Topic
from .config import Config
from .curator import collect
from .logs import setup as setup_logging
from .mobbin_auth import get_access_token
from .models import Digest, Pick, Section
from . import chats as chats_file
from .profile import WEEKDAY_NAMES, Day, ProfileError, Slot
from .profile import load as load_schedule
from .profile import plan_for_day
from .render import caption_html, digest_keyboard, header_html, pick_keyboard
from .setup_cli import describe
from .state import SeenScreen, Store
from .telegram import MEDIA_GROUP_LIMIT, Telegram, TelegramError, _message_id

log = logging.getLogger("inspobot")


def today_in(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


async def _one_by_one(
    telegram: Telegram,
    urls: Sequence[str],
    chat_id: str | None,
    as_document: bool,
) -> list[int]:
    """Запасной путь, когда галерея не ушла: по одному файлу."""
    ids: list[int] = []
    for url in urls:
        await telegram.pause()
        mid = await telegram.send_media(url, "", as_document=as_document, chat_id=chat_id)
        if mid is not None:
            ids.append(mid)
    return ids


async def send_pick(
    telegram: Telegram,
    section: Section,
    pick: Pick,
    index: int,
    total: int,
    chat_id: str | None,
    as_document: bool,
) -> list[int]:
    """Отправить находку. Возвращает номера сообщений — первым идёт то, что с
    подписью и кнопкой, дальше шаги галереи. По ним топ потом копирует
    находку, не перекачивая файлы."""
    caption = caption_html(section, pick, index, total)
    keyboard = pick_keyboard(pick)
    images = pick.images
    ids: list[int] = []

    if len(images) == 1:
        await telegram.pause()
        mid = await telegram.send_media(
            images[0], caption, as_document=as_document, keyboard=keyboard, chat_id=chat_id
        )
        if mid is None:
            # Файл не дошёл — текст с кнопкой всё равно нужен.
            mid = _message_id(
                await telegram.send_message(caption, chat_id=chat_id, keyboard=keyboard)
            )
        return [mid] if mid is not None else []

    # Флоу. Подпись с кнопкой уходит отдельным сообщением перед галереей:
    # sendMediaGroup не принимает reply_markup, кнопку к нему не прицепить.
    await telegram.pause()
    mid = _message_id(await telegram.send_message(caption, chat_id=chat_id, keyboard=keyboard))
    if mid is not None:
        ids.append(mid)

    # Все шаги галереей. В одну влезает десять, длинные сценарии разбиваются.
    for chunk in _chunks(images, MEDIA_GROUP_LIMIT):
        await telegram.pause()
        got = await telegram.send_media_group(
            chunk, "", as_document=as_document, chat_id=chat_id
        )
        if not got:
            got = await _one_by_one(telegram, chunk, chat_id, as_document)
        ids.extend(got)
    return ids


async def send_section(
    telegram: Telegram,
    section: Section,
    chat_id: str | None = None,
    as_document: bool = False,
) -> dict[str, list[int]]:
    """Номера сообщений по каждой находке блока, ключ — screen_id."""
    total = len(section.picks)
    delivered: dict[str, list[int]] = {}
    for index, pick in enumerate(section.picks, start=1):
        delivered[pick.screen_id] = await send_pick(
            telegram, section, pick, index, total, chat_id, as_document
        )
    return delivered


async def deliver(
    telegram: Telegram,
    digest: Digest,
    chat_id: str | None = None,
    as_document: bool = False,
    top_buttons: bool = False,
) -> dict[str, list[int]]:
    """Вся подборка в один чат. Возвращает номера сообщений по находкам."""
    await telegram.send_message(
        header_html(digest),
        chat_id=chat_id,
        keyboard=digest_keyboard() if top_buttons else None,
    )
    delivered: dict[str, list[int]] = {}
    for section in digest.sections:
        delivered.update(await send_section(telegram, section, chat_id, as_document))
    return delivered


async def build_digest(
    config: Config,
    store: Store,
    day: date,
    day_plan: Day,
    plan: Sequence[tuple[Slot, Topic]],
) -> Digest:
    log.info(
        "%s — %s: %s",
        WEEKDAY_NAMES[day.weekday()],
        day_plan.title or "без названия",
        "; ".join(f"{slot.title()} → {topic.title}" for slot, topic in plan),
    )
    token = get_access_token(
        config.mobbin_token_file, config.mobbin_mcp_url, config.mobbin_access_token
    )
    seen_by_platform = {
        "ios": store.recent_seen_ids("ios"),
        "web": store.recent_seen_ids("web"),
    }
    return await asyncio.to_thread(
        collect, config, day, plan, token, seen_by_platform, day_plan.title
    )


def recipients(config: Config, only: str | None = None) -> tuple[str, ...]:
    if only:
        return (only,)
    return chats_file.load(config.chats_path, config.telegram_chat_id)


def remember_picks(store: Store, digest: Digest) -> dict[str, int]:
    """Записать находки с оценками; вернуть id по screen_id."""
    ids: dict[str, int] = {}
    for section in digest.sections:
        for pick in section.picks:
            ids[pick.screen_id] = store.record_pick(
                day=digest.day,
                kind=section.slot.kind,
                platform=pick.platform,
                screen_id=pick.screen_id,
                app_name=pick.app_name,
                pattern=pick.pattern,
                note=pick.note,
                mobbin_url=pick.mobbin_url,
                topic=section.topic.title,
                block=section.slot.title(),
                score=pick.score,
            )
    return ids


async def broadcast(
    telegram: Telegram,
    digest: Digest,
    targets: Sequence[str],
    as_document: bool,
    *,
    store: Store | None = None,
    pick_ids: dict[str, int] | None = None,
    top_buttons: bool = False,
) -> tuple[int, list[str]]:
    """Одна и та же подборка во все чаты. Упавший чат не роняет рассылку.

    Если передан `store`, номера сообщений каждой находки запоминаются
    по чатам — из них потом собирается топ.
    """
    sent = 0
    failed: list[str] = []
    for target in targets:
        try:
            delivered = await deliver(
                telegram, digest, chat_id=target, as_document=as_document,
                top_buttons=top_buttons,
            )
        except TelegramError as exc:
            log.warning("Чат %s не получил подборку: %s", target, exc)
            failed.append(target)
            continue
        sent += len(delivered)
        if store is not None and pick_ids:
            for screen_id, message_ids in delivered.items():
                if screen_id in pick_ids:
                    store.record_delivery(pick_ids[screen_id], target, message_ids)
    return sent, failed


async def run_once(
    config: Config,
    *,
    day: date | None = None,
    force: bool = False,
    dry_run: bool = False,
    chat_id: str | None = None,
) -> int:
    store = Store(config.db_path)
    day = day or today_in(config.timezone)

    planned = plan_for_day(load_schedule(config.profile_path), day)
    if planned is None:
        log.info("%s — в этот день письма нет", WEEKDAY_NAMES[day.weekday()])
        return 0
    day_plan, plan = planned

    if store.sent_today(day) and not force:
        log.info("Подборка на %s уже уходила — пропускаю", day.isoformat())
        return 0

    run_id = store.start_run(day)
    try:
        digest = await build_digest(config, store, day, day_plan, plan)
    except Exception as exc:  # noqa: BLE001 — любой сбой должен попасть в лог и в чат
        store.finish_run(run_id, ok=False, error=repr(exc))
        log.exception("Подборка не собралась")
        if not dry_run and config.telegram_token and config.telegram_chat_id:
            async with Telegram(config.telegram_token, config.telegram_chat_id) as tg:
                await tg.send_message(
                    f"Утренняя подборка сегодня не собралась.\n<code>{type(exc).__name__}: {exc}</code>"[:3500],
                    chat_id=chat_id,
                )
        raise

    if dry_run:
        print(header_html(digest))
        for section in digest.sections:
            for index, pick in enumerate(section.picks, start=1):
                print("---")
                print(caption_html(section, pick, index, len(section.picks)))
                for image in pick.images:
                    print(image)
        store.finish_run(run_id, ok=False, picks=len(digest.picks), error="dry-run")
        return len(digest.picks)

    targets = recipients(config, chat_id)
    if not targets:
        store.finish_run(run_id, ok=False, error="некому отправлять")
        raise TelegramError(
            "Список получателей пуст: заполните TELEGRAM_CHAT_ID или "
            f"{config.chats_path}"
        )

    pick_ids = remember_picks(store, digest)
    async with Telegram(config.telegram_token, config.telegram_chat_id) as tg:
        sent, failed = await broadcast(
            tg,
            digest,
            targets,
            config.image_mode == "document",
            store=store,
            pick_ids=pick_ids,
            top_buttons=config.top_buttons,
        )
        if len(failed) == len(targets):
            error = f"ни один из {len(targets)} чатов не принял подборку"
            store.finish_run(run_id, ok=False, error=error)
            raise TelegramError(error)
        if failed:
            log.warning("Не доставлено в %d из %d чатов: %s", len(failed), len(targets), failed)

    store.mark_seen(
        [
            SeenScreen(p.screen_id, p.platform, p.app_name, p.mobbin_url)
            for p in digest.screen_picks
        ],
        day,
    )
    store.finish_run(run_id, ok=True, picks=sent)
    log.info("Отправлено находок: %d", sent)
    return sent


def show_plan(config: Config, day: date) -> int:
    """Что уйдёт на неделе — без запроса к API и без отправки."""
    try:
        schedule = load_schedule(config.profile_path)
    except ProfileError as exc:
        print(f"Расписание не читается: {exc}", file=sys.stderr)
        return 1

    targets = recipients(config)
    print(f"Расписание: {config.profile_path}")
    print(f"Получателей: {len(targets)}\n")
    print(describe(schedule, day))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Собрать и отправить утреннюю подборку")
    parser.add_argument("--force", action="store_true", help="отправить, даже если сегодня уже слали")
    parser.add_argument("--dry-run", action="store_true", help="показать в консоли, ничего не отправлять")
    parser.add_argument("--plan", action="store_true", help="показать профиль и темы дня, не обращаясь к API")
    parser.add_argument("--day", help="дата в формате ГГГГ-ММ-ДД (по умолчанию сегодня)")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="подробный лог самого бота (тела запросов к API не печатаются)",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    config = Config.from_env()
    day = date.fromisoformat(args.day) if args.day else today_in(config.timezone)

    if args.plan:
        return show_plan(config, day)

    required = ("anthropic_api_key",) if args.dry_run else (
        "anthropic_api_key", "telegram_token", "telegram_chat_id"
    )
    config.require(*required)

    try:
        asyncio.run(
            run_once(config, day=day, force=args.force or bool(args.day), dry_run=args.dry_run)
        )
    except Exception as exc:  # noqa: BLE001 — cron читает только код возврата
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
