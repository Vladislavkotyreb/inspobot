"""Самопроверка доступов: `python -m inspobot.doctor`.

Три ключа и один OAuth-токен — четыре места, где всё может пойти не так.
Команда проверяет каждое по отдельности и говорит, что именно чинить, вместо
одной общей ошибки в шесть утра.

    python -m inspobot.doctor              # проверить
    python -m inspobot.doctor --send-test  # ещё и написать в чат
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .config import Config
from .mobbin_auth import load_tokens
from .telegram import Telegram

OK = "✓"
FAIL = "✗"
WARN = "!"


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    detail: str

    def line(self) -> str:
        return f"{self.status} {self.name}: {self.detail}"


def chats_from_updates(updates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Уникальные чаты из свежих апдейтов — чтобы не искать chat_id вручную."""
    chats: dict[str, dict[str, Any]] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        title = chat.get("title") or " ".join(
            part for part in (chat.get("first_name"), chat.get("last_name")) if part
        ) or chat.get("username") or ""
        chats[str(chat_id)] = {
            "id": str(chat_id),
            "type": chat.get("type", "?"),
            "title": title,
        }
    return list(chats.values())


async def check_telegram(config: Config) -> list[Result]:
    if not config.telegram_token:
        return [Result("Telegram", FAIL, "не задан TELEGRAM_BOT_TOKEN")]

    results: list[Result] = []
    async with Telegram(config.telegram_token, config.telegram_chat_id) as telegram:
        try:
            me = await telegram.get_me()
        except Exception as exc:  # noqa: BLE001
            return [Result("Telegram", FAIL, f"токен не принят: {exc}")]
        results.append(
            Result("Telegram", OK, f"бот @{me.get('username', '?')} (id {me.get('id')})")
        )

        if config.telegram_chat_id:
            results.append(Result("chat_id", OK, config.telegram_chat_id))
            return results

        try:
            chats = chats_from_updates(await telegram.get_updates(0, timeout=0))
        except Exception as exc:  # noqa: BLE001
            return results + [Result("chat_id", WARN, f"не задан, getUpdates не ответил: {exc}")]

        if not chats:
            return results + [
                Result(
                    "chat_id",
                    FAIL,
                    "не задан. Напишите боту любое сообщение и запустите проверку снова",
                )
            ]
        listed = ", ".join(f"{c['id']} ({c['type']}, {c['title']})".strip() for c in chats)
        return results + [
            Result("chat_id", WARN, f"не задан. Подходит: {listed}")
        ]


def check_anthropic(config: Config) -> Result:
    if not config.anthropic_api_key:
        return Result("Anthropic", FAIL, "не задан ANTHROPIC_API_KEY")
    try:
        from .client import make_client

        info = make_client(config).models.retrieve(config.model)
    except Exception as exc:  # noqa: BLE001
        return Result("Anthropic", FAIL, explain_anthropic_error(exc))
    suffix = " (workspace задан)" if config.anthropic_workspace_id else ""
    return Result("Anthropic", OK, f"модель доступна: {info.id}{suffix}")


def explain_anthropic_error(exc: Exception) -> str:
    """Частые отказы API — человеческим языком вместо сырого JSON."""
    text = str(exc)
    if "not scoped to a workspace" in text or "anthropic-workspace-id" in text:
        return (
            "ключ выпущен на уровне организации и не привязан к workspace. "
            "Либо создайте в консоли ключ внутри нужного workspace, "
            "либо добавьте в .env строку ANTHROPIC_WORKSPACE_ID=wrkspc_… "
            "(id виден в адресной строке консоли на странице workspace)"
        )
    if "credit balance is too low" in text or "insufficient" in text.lower():
        return "на балансе нет средств — пополните его в консоли, раздел Billing"
    if "invalid x-api-key" in text or "authentication_error" in text:
        return "ключ не принят — проверьте строку 8 в .env, он должен начинаться с sk-ant-"
    return f"{type(exc).__name__}: {text}"


def check_mobbin(config: Config) -> Result:
    if config.mobbin_access_token:
        return Result("Mobbin", OK, "используется MOBBIN_ACCESS_TOKEN из окружения")
    try:
        tokens = load_tokens(config.mobbin_token_file)
    except Exception as exc:  # noqa: BLE001
        return Result("Mobbin", FAIL, str(exc))
    if tokens is None:
        return Result(
            "Mobbin",
            FAIL,
            f"нет {config.mobbin_token_file} — пройдите `python -m inspobot.auth_cli`",
        )
    left = tokens.expires_at - datetime.now(timezone.utc).timestamp()
    if tokens.refresh_token:
        if left <= 0:
            # Раньше здесь стояла галочка: файл есть, refresh-токен есть — значит
            # всё хорошо. Но обновление могло и не пройти: Supabase гасит старый
            # refresh-токен, и если им уже воспользовались в другом месте, сбой
            # вылезет только на боевом запуске. Честнее предупредить.
            return Result(
                "Mobbin",
                WARN,
                "access-токен истёк; обновится при первом запросе — "
                "если refresh-токен не израсходован где-то ещё",
            )
        return Result("Mobbin", OK, f"токен есть, обновляется сам (осталось {left / 60:.0f} мин)")
    if left <= 0:
        return Result("Mobbin", FAIL, "токен истёк, refresh-токена нет — нужен повторный вход")
    return Result("Mobbin", WARN, f"токен без refresh, истечёт через {left / 60:.0f} мин")


async def run(config: Config, send_test: bool = False) -> int:
    results = await check_telegram(config)
    results.append(check_anthropic(config))
    results.append(check_mobbin(config))

    for result in results:
        print(result.line())

    if send_test and config.telegram_token and config.telegram_chat_id:
        async with Telegram(config.telegram_token, config.telegram_chat_id) as telegram:
            try:
                await telegram.send_message("<b>inspobot</b> на связи. Проверка прошла.")
                print(f"{OK} тестовое сообщение отправлено")
            except Exception as exc:  # noqa: BLE001
                print(f"{FAIL} тестовое сообщение не ушло: {exc}")
                return 1

    return 1 if any(r.status == FAIL for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить доступы inspobot")
    parser.add_argument("--send-test", action="store_true", help="написать в чат тестовое сообщение")
    args = parser.parse_args(argv)
    return asyncio.run(run(Config.from_env(), send_test=args.send_test))


if __name__ == "__main__":
    raise SystemExit(main())
