"""Долгоживущий бот: расписание внутри процесса плюс команды и пошаговый подбор.

Альтернатива системному cron. Полезно, если на хостинге удобнее держать один
демон, чем прописывать crontab, — и обязательно, если нужен `/pick`.

    /pick    — подобрать референсы по шагам
    /now     — дневная подборка прямо сейчас
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

from . import render, wizard
from .config import Config
from .curator import collect_selection
from .daily import run_once, send_picks, today_in
from .logs import setup as setup_logging
from .mobbin_auth import get_access_token
from .state import SeenScreen, Store
from .telegram import Telegram
from .topics import topics_for
from .wizard import Selection

log = logging.getLogger("inspobot.bot")

HELP = (
    "<b>inspobot</b> — интерфейсные референсы из Mobbin.\n\n"
    "/pick — подобрать по шагам: экраны, флоу или секции сайта, "
    "мобилка или десктоп, B2B или B2C, тема\n"
    "/now — дневная подборка сейчас\n"
    "/topics — темы на сегодня и завтра\n"
    "/id — id этого чата\n"
    "/help — эта справка"
)

STALE_BUTTON = "Кнопка из старого сообщения — наберите /pick"


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


# --- пошаговый подбор -------------------------------------------------------


async def start_wizard(telegram: Telegram, chat_id: str) -> None:
    selection = Selection()
    step = wizard.next_step(selection)
    assert step is not None  # пустой выбор всегда с чего-то начинается
    await telegram.send_message_with_keyboard(
        render.wizard_text(step, selection),
        render.wizard_keyboard(step, selection),
        chat_id=chat_id,
    )


async def run_selection(
    config: Config, telegram: Telegram, selection: Selection, chat_id: str, message_id: int
) -> None:
    crumbs = escape(wizard.breadcrumb(selection))
    await telegram.edit_message(chat_id, message_id, f"<b>{crumbs}</b>\n\nИщу…")

    store = Store(config.db_path)
    platform = wizard.api_platform(selection)
    seen = store.recent_seen_ids(platform) if selection.kind == "s" else []

    token = get_access_token(
        config.mobbin_token_file, config.mobbin_mcp_url, config.mobbin_access_token
    )
    summary, picks = await asyncio.to_thread(
        collect_selection, config, selection, token, config.picks_per_platform, seen
    )

    await telegram.edit_message(
        chat_id, message_id, render.selection_header(selection, summary, len(picks))
    )
    await send_picks(telegram, picks, chat_id)

    if selection.kind == "s":
        # Экраны из подбора тоже больше не повторяем в утренней подборке.
        store.mark_seen(
            [SeenScreen(p.screen_id, p.platform, p.app_name, p.mobbin_url) for p in picks],
            today_in(config.timezone),
        )


async def handle_callback(config: Config, telegram: Telegram, query: dict[str, Any]) -> None:
    message = query.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")

    if config.telegram_chat_id and chat_id != config.telegram_chat_id:
        await telegram.answer_callback(query["id"], "Этот бот отвечает другому чату")
        return

    selection = wizard.decode(str(query.get("data", "")))
    if selection is None or message_id is None:
        await telegram.answer_callback(query["id"], STALE_BUTTON)
        return

    await telegram.answer_callback(query["id"])

    step = wizard.next_step(selection)
    if step is not None:
        await telegram.edit_message(
            chat_id,
            message_id,
            render.wizard_text(step, selection),
            render.wizard_keyboard(step, selection),
        )
        return

    try:
        await run_selection(config, telegram, selection, chat_id, message_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Подбор сорвался")
        await telegram.edit_message(
            chat_id, message_id, f"Не получилось: {escape(str(exc))[:500]}"
        )


# --- команды ----------------------------------------------------------------


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
    elif command == "/pick":
        await start_wizard(telegram, chat_id)
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
            try:
                if "callback_query" in update:
                    await handle_callback(config, telegram, update["callback_query"])
                    continue
                message = update.get("message") or update.get("channel_post")
                if message and str(message.get("text", "")).startswith("/"):
                    await handle_command(config, telegram, message)
            except Exception:  # noqa: BLE001
                log.exception("Обработка апдейта сорвалась")


async def amain() -> None:
    config = Config.from_env()
    config.require("anthropic_api_key", "telegram_token", "telegram_chat_id")
    async with Telegram(config.telegram_token, config.telegram_chat_id) as telegram:
        await asyncio.gather(scheduler(config), poll(config, telegram))


def main() -> int:
    setup_logging()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
