"""Долгоживущий бот: расписание внутри процесса плюс команды в чате.

Альтернатива системному cron. Полезно, если на хостинге удобнее держать один
демон, чем прописывать crontab. Команды:

    /now     — собрать подборку прямо сейчас
    /topics  — темы на сегодня и завтра
    /id      — chat_id текущего чата (нужен при первичной настройке)
    /help    — то же самое списком
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .daily import run_once, today_in
from .telegram import Telegram
from .topics import topics_for

log = logging.getLogger("inspobot.bot")

HELP = (
    "<b>inspobot</b> — утренняя подборка интерфейсных референсов из Mobbin.\n\n"
    "/now — собрать подборку сейчас\n"
    "/topics — темы на сегодня и завтра\n"
    "/id — id этого чата\n"
    "/help — эта справка"
)


def next_run_at(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def scheduler(config: Config) -> None:
    tz = ZoneInfo(config.timezone)
    while True:
        now = datetime.now(tz)
        target = next_run_at(now, config.hour, config.minute)
        delay = (target - now).total_seconds()
        log.info("Следующая подборка: %s (через %.0f мин)", target.isoformat(), delay / 60)
        await asyncio.sleep(delay)
        try:
            await run_once(config)
        except Exception:  # noqa: BLE001 — расписание не должно умирать от одной ошибки
            log.exception("Плановая подборка сорвалась")


async def handle_command(config: Config, telegram: Telegram, message: dict[str, Any]) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = str(message.get("text", "")).strip()
    command = text.split()[0].split("@")[0].lower() if text else ""

    if command == "/id":
        await telegram.send_message(f"chat_id: <code>{escape(chat_id)}</code>", chat_id=chat_id)
        return

    if config.telegram_chat_id and chat_id != config.telegram_chat_id:
        log.info("Игнорирую сообщение из чужого чата %s", chat_id)
        return

    if command in ("/start", "/help"):
        await telegram.send_message(HELP, chat_id=chat_id)
    elif command == "/topics":
        today = today_in(config.timezone)
        lines = []
        for label, day in (("Сегодня", today), ("Завтра", today + timedelta(days=1))):
            mobile, desktop = topics_for(day)
            lines.append(
                f"<b>{label}</b>\n📱 {escape(mobile.title)}\n🖥 {escape(desktop.title)}"
            )
        await telegram.send_message("\n\n".join(lines), chat_id=chat_id)
    elif command == "/now":
        await telegram.send_message("Собираю подборку, это займёт минуту…", chat_id=chat_id)
        try:
            await run_once(config, force=True, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("/now не отработал")
            await telegram.send_message(f"Не получилось: {escape(str(exc))[:500]}", chat_id=chat_id)


async def poll(config: Config, telegram: Telegram) -> None:
    offset = 0
    while True:
        try:
            updates = await telegram.get_updates(offset)
        except Exception as exc:  # noqa: BLE001 — сеть отвалилась, ждём и пробуем снова
            log.warning("getUpdates: %s", exc)
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = max(offset, int(update["update_id"]) + 1)
            message = update.get("message") or update.get("channel_post")
            if message and message.get("text", "").startswith("/"):
                try:
                    await handle_command(config, telegram, message)
                except Exception:  # noqa: BLE001
                    log.exception("Команда сорвалась")


async def amain() -> None:
    config = Config.from_env()
    config.require("anthropic_api_key", "telegram_token", "telegram_chat_id")
    async with Telegram(config.telegram_token, config.telegram_chat_id) as telegram:
        await asyncio.gather(scheduler(config), poll(config, telegram))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
