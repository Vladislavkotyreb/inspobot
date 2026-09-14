"""Служба кнопок: принимает нажатия и присылает топ.

Дайджест живёт по расписанию и между запусками не существует, а нажатие на
кнопку — это входящее событие, которое кто-то должен принять. На сервере,
который и так работает круглосуточно, для этого хватает маленькой службы:
она держит длинный опрос Telegram и на нажатие запускает тот же
`inspobot.top`. Ни Claude, ни Mobbin при этом не трогаются — топ собирается
из базы, так что нажатия ничего не стоят.

    python -m inspobot.listener

Ставится как systemd-юнит, см. deploy/inspobot-listener.service.
Кнопки под шапкой дайджеста включаются переменной INSPOBOT_TOP_BUTTONS=1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Config
from .daily import recipients
from .logs import setup as setup_logging
from .render import MONTH_BUTTON, TOP_MONTH, TOP_WEEK, WEEK_BUTTON, top_reply_keyboard
from .telegram import Telegram
from .top import run as run_top

log = logging.getLogger("inspobot.listener")

PERIOD_BY_DATA = {TOP_WEEK: "week", TOP_MONTH: "month"}
PERIOD_BY_COMMAND = {"/week": "week", "/month": "month"}
# Нажатие на постоянную клавиатуру приходит обычным сообщением с текстом
# кнопки — узнаём её по нему.
PERIOD_BY_BUTTON = {WEEK_BUTTON: "week", MONTH_BUTTON: "month"}

BUSY = "Уже собираю, подождите немного"
STALE = "Кнопка не опознана — наберите /week или /month"
FOREIGN = "Этот бот отвечает другому чату"
STARTED = "Собираю топ"

HELP = (
    "<b>inspobot</b>\n\n"
    "Каждое утро в 11:00 присылаю подборку интерфейсных референсов из Mobbin. "
    "У каждого дня своя тема: понедельник — геймификация, вторник — сложные "
    "процессы, среда — пейволлы и оплата, четверг — таблицы и дашборды, "
    "пятница — лендинги, воскресенье — онбординг.\n\n"
    "Кнопки ниже показывают лучшее из уже присланного."
)

POLL_ERROR_PAUSE = 5


def allowed(config: Config, chat_id: str) -> bool:
    """Кнопки слушаем только из своих чатов — иначе любой желающий сможет
    гонять рассылку по чужим адресам."""
    return chat_id in recipients(config)


async def send_top(config: Config, period: str, chat_id: str, lock: asyncio.Lock) -> None:
    if lock.locked():
        log.info("Топ уже собирается, нажатие пропущено")
        return
    async with lock:
        try:
            await run_top(config, period, chat_id=chat_id)
        except Exception:  # noqa: BLE001 — служба не должна умирать от одного сбоя
            log.exception("Топ %s не собрался", period)


async def handle_update(
    config: Config, telegram: Telegram, update: dict[str, Any], lock: asyncio.Lock
) -> None:
    query = update.get("callback_query")
    if query:
        chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
        period = PERIOD_BY_DATA.get(str(query.get("data", "")))
        if not allowed(config, chat_id):
            await telegram.answer_callback(query["id"], FOREIGN)
            return
        if period is None:
            await telegram.answer_callback(query["id"], STALE)
            return
        await telegram.answer_callback(query["id"], BUSY if lock.locked() else STARTED)
        await send_top(config, period, chat_id, lock)
        return

    message = update.get("message") or update.get("channel_post")
    if not message:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = str(message.get("text", "")).strip()
    if not text or not allowed(config, chat_id):
        return

    # Нажатие постоянной клавиатуры приходит как обычный текст.
    period = PERIOD_BY_BUTTON.get(text)
    if period is None:
        command = text.split()[0].split("@")[0].lower()
        period = PERIOD_BY_COMMAND.get(command)
        if period is None:
            if command in ("/start", "/help", "/buttons"):
                # Клавиатура ставится вместе с ответом: показать её иначе
                # нельзя, она приходит только приложением к сообщению.
                await telegram.send_message(
                    HELP, chat_id=chat_id, keyboard=top_reply_keyboard()
                )
            return

    await telegram.send_message(f"{STARTED}…", chat_id=chat_id)
    await send_top(config, period, chat_id, lock)


async def poll(config: Config, telegram: Telegram) -> None:
    offset = 0
    lock = asyncio.Lock()
    log.info("Слушаю нажатия. Получателей: %d", len(recipients(config)))
    while True:
        try:
            updates = await telegram.get_updates(offset)
        except Exception as exc:  # noqa: BLE001 — сеть отвалилась, ждём и пробуем снова
            log.warning("getUpdates: %s", exc)
            await asyncio.sleep(POLL_ERROR_PAUSE)
            continue
        for update in updates:
            offset = max(offset, int(update["update_id"]) + 1)
            try:
                await handle_update(config, telegram, update, lock)
            except Exception:  # noqa: BLE001
                log.exception("Нажатие не обработано")


async def amain() -> None:
    config = Config.from_env()
    config.require("telegram_token", "telegram_chat_id")
    async with Telegram(config.telegram_token, config.telegram_chat_id) as telegram:
        await poll(config, telegram)


def main() -> int:
    setup_logging()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
