"""Одна утренняя подборка: собрать и отправить.

Точка входа для системного cron:

    python -m inspobot.daily

Повторный запуск в тот же день ничего не отправит (если не передать --force),
поэтому «дёрнуть ещё раз после сбоя» безопасно.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import Config
from .logs import setup as setup_logging
from .curator import collect
from .models import Digest
from .mobbin_auth import get_access_token
from .render import caption_html, header_html
from .state import SeenScreen, Store
from .telegram import Telegram, TelegramError
from .topics import topics_for

log = logging.getLogger("inspobot")


def today_in(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


async def deliver(telegram: Telegram, digest: Digest, chat_id: str | None = None) -> int:
    await telegram.send_message(header_html(digest), chat_id=chat_id)
    sent = 0
    for platform in ("ios", "web"):
        picks = digest.by_platform(platform)
        for index, pick in enumerate(picks, start=1):
            await telegram.pause()
            caption = caption_html(pick, index, len(picks))
            ok = await telegram.send_photo(pick.image_url, caption, chat_id=chat_id)
            if not ok:
                # Telegram не забрал превью — тот же текст, но без картинки.
                await telegram.send_message(caption, chat_id=chat_id)
            sent += 1
    return sent


async def build_digest(config: Config, store: Store, day: date) -> Digest:
    mobile, desktop = topics_for(day)
    token = get_access_token(
        config.mobbin_token_file, config.mobbin_mcp_url, config.mobbin_access_token
    )
    log.info("Тема дня: %s / %s", mobile.title, desktop.title)
    return await asyncio.to_thread(
        collect,
        config,
        day,
        mobile,
        desktop,
        token,
        store.recent_seen_ids("ios"),
        store.recent_seen_ids("web"),
    )


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

    if store.sent_today(day) and not force:
        log.info("Подборка на %s уже уходила — пропускаю", day.isoformat())
        return 0

    run_id = store.start_run(day)
    try:
        digest = await build_digest(config, store, day)
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
        for platform in ("ios", "web"):
            picks = digest.by_platform(platform)
            for index, pick in enumerate(picks, start=1):
                print("---")
                print(caption_html(pick, index, len(picks)))
                print(pick.image_url)
        store.finish_run(run_id, ok=False, picks=len(digest.picks), error="dry-run")
        return len(digest.picks)

    async with Telegram(config.telegram_token, config.telegram_chat_id) as tg:
        try:
            sent = await deliver(tg, digest, chat_id=chat_id)
        except TelegramError as exc:
            store.finish_run(run_id, ok=False, error=repr(exc))
            raise

    store.mark_seen(
        [
            SeenScreen(p.screen_id, p.platform, p.app_name, p.mobbin_url)
            for p in digest.picks
        ],
        day,
    )
    store.finish_run(run_id, ok=True, picks=sent)
    log.info("Отправлено экранов: %d", sent)
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Собрать и отправить утреннюю подборку")
    parser.add_argument("--force", action="store_true", help="отправить, даже если сегодня уже слали")
    parser.add_argument("--dry-run", action="store_true", help="показать в консоли, ничего не отправлять")
    parser.add_argument("--day", help="дата в формате ГГГГ-ММ-ДД (по умолчанию сегодня)")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="подробный лог самого бота (тела запросов к API не печатаются)",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    config = Config.from_env()
    required = ("anthropic_api_key",) if args.dry_run else (
        "anthropic_api_key", "telegram_token", "telegram_chat_id"
    )
    config.require(*required)

    day = date.fromisoformat(args.day) if args.day else None
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
