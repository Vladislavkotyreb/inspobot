"""Топ находок за период: `python -m inspobot.top --week` или `--month`.

Ни одного обращения к Claude или Mobbin: оценки поставлены в момент отбора
и лежат в базе, а картинки Telegram копирует из уже отправленных сообщений
(`copyMessage`) — файл тот же, качество то же, баланс не тратится.

Запускается руками, по расписанию или от кнопки в чате через ретранслятор —
см. docs/TOP.md.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

from .config import Config
from .daily import recipients, today_in
from .logs import setup as setup_logging
from .render import pick_keyboard, top_caption_html, top_header_html
from .state import Store, StoredPick
from .telegram import Telegram, TelegramError

log = logging.getLogger("inspobot.top")

PERIOD_DAYS = {"week": 7, "month": 30}
DEFAULT_LIMIT = 10


def period_bounds(period: str, today: date) -> tuple[date, date]:
    """Окно «за неделю» — последние семь дней включая сегодня."""
    return today - timedelta(days=PERIOD_DAYS[period] - 1), today


async def send_top_pick(
    telegram: Telegram, store: Store, pick: StoredPick, rank: int, chat_id: str
) -> bool:
    """Одна находка в топе. Если в этот чат она уходила — копируем то самое
    сообщение; если нет (новый подписчик) — текстовая карточка с кнопкой."""
    caption = top_caption_html(rank, pick)
    keyboard = pick_keyboard(pick)
    ids = store.delivery(pick.id, chat_id)

    if not ids:
        await telegram.send_message(caption, chat_id=chat_id, keyboard=keyboard)
        return False

    if pick.kind == "f" and len(ids) > 1:
        # Флоу: подпись новым сообщением (текст через copyMessage не подменить),
        # шаги — копией галереи целиком.
        await telegram.send_message(caption, chat_id=chat_id, keyboard=keyboard)
        ok = await telegram.copy_messages(chat_id, ids[1:], to_chat_id=chat_id)
        return ok

    ok = await telegram.copy_message(
        chat_id, ids[0], to_chat_id=chat_id, caption=caption, keyboard=keyboard
    )
    if not ok:
        # Сообщение могли удалить — карточка с кнопкой всё равно уйдёт.
        await telegram.send_message(caption, chat_id=chat_id, keyboard=keyboard)
    return ok


async def run(
    config: Config,
    period: str,
    *,
    chat_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    today: date | None = None,
) -> int:
    store = Store(config.db_path)
    today = today or today_in(config.timezone)
    since, until = period_bounds(period, today)
    picks = store.top(since, until, limit)
    log.info("Топ %s: %d находок за %s — %s", period, len(picks), since, until)

    targets = recipients(config, chat_id)
    if not targets:
        raise TelegramError("Список получателей пуст.")

    async with Telegram(config.telegram_token, config.telegram_chat_id) as tg:
        for target in targets:
            try:
                await tg.send_message(
                    top_header_html(period, since, until, len(picks)), chat_id=target
                )
                for rank, pick in enumerate(picks, start=1):
                    await tg.pause()
                    await send_top_pick(tg, store, pick, rank, target)
            except TelegramError as exc:
                log.warning("Чат %s не получил топ: %s", target, exc)
    return len(picks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Топ находок за период, без обращений к API")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--week", action="store_const", dest="period", const="week")
    group.add_argument("--month", action="store_const", dest="period", const="month")
    parser.add_argument("--chat", help="только в этот чат (по умолчанию — всем получателям)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    config = Config.from_env()
    config.require("telegram_token", "telegram_chat_id")
    try:
        count = asyncio.run(run(config, args.period, chat_id=args.chat, limit=args.limit))
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Отправлено находок: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
