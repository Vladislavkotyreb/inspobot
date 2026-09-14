"""Отправка в Telegram напрямую по Bot API — без фреймворка.

Из зависимостей только httpx: на shared-хостинге чем меньше пакетов, тем
меньше поводов для сюрпризов при обновлении Python.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Sequence

import httpx

API_ROOT = "https://api.telegram.org"
SEND_PAUSE = 0.6  # Telegram: не больше ~20 сообщений в минуту в один чат
MEDIA_GROUP_LIMIT = 10  # столько картинок влезает в одну галерею
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


def _message_id(result: Any) -> int | None:
    if isinstance(result, dict):
        try:
            return int(result["message_id"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=TIMEOUT)

    async def __aenter__(self) -> "Telegram":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.token}/{method}"

    async def _call(
        self, method: str, data: dict[str, Any], files: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(self._url(method), data=data, files=files)
        except httpx.HTTPError as exc:
            # Превращаем сетевой сбой в свою ошибку: у таймаутов httpx текст
            # часто пустой, и наружу уходило «Ошибка:» без единого слова.
            raise TelegramError(
                f"{method}: не достучались до Telegram ({type(exc).__name__}"
                + (f": {exc}" if str(exc) else "")
                + ")"
            ) from exc
        payload: dict[str, Any]
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: не JSON ({response.status_code})") from exc
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload.get('description', response.status_code)}")
        return payload["result"]

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe", {})

    async def send_message(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard)
        return await self._call("sendMessage", data)

    async def send_media(
        self,
        image_url: str,
        caption: str,
        *,
        as_document: bool = False,
        keyboard: dict[str, Any] | None = None,
        chat_id: str | None = None,
    ) -> int | None:
        """Одна картинка: файлом или фотографией, с кнопками под ней.

        `as_document=True` шлёт исходный файл — Telegram его не сжимает, и
        мелкий текст на экране остаётся читаемым.

        Сначала пробуем отдать ссылку Telegram, потом качаем сами. Возвращает
        номер отправленного сообщения — он нужен, чтобы потом скопировать его
        в топ, — или None, если доставить не вышло: вызывающий код тогда шлёт
        тот же текст без изображения.
        """
        method = "sendDocument" if as_document else "sendPhoto"
        field = "document" if as_document else "photo"
        base: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if keyboard:
            base["reply_markup"] = json.dumps(keyboard)

        try:
            result = await self._call(method, {**base, field: image_url})
            return _message_id(result)
        except TelegramError as exc:
            log.info("%s по ссылке не прошёл (%s), качаю сам", method, exc)

        try:
            blob = await self._client.get(image_url, follow_redirects=True)
            blob.raise_for_status()
            content_type = blob.headers.get("content-type", "image/jpeg")
            suffix = "webp" if "webp" in content_type else "jpg"
            result = await self._call(
                method,
                base,
                files={field: (f"screen.{suffix}", blob.content, content_type)},
            )
            return _message_id(result)
        except (httpx.HTTPError, TelegramError) as exc:
            log.warning("Не удалось отправить %s: %s", image_url, exc)
            return None

    async def pause(self) -> None:
        await asyncio.sleep(SEND_PAUSE)

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        try:
            response = await self._client.post(
                self._url("getUpdates"),
                data={"offset": offset, "timeout": timeout},
                timeout=httpx.Timeout(timeout + 20.0, connect=15.0),
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"getUpdates: {type(exc).__name__}: {exc}") from exc
        payload = response.json()
        if not payload.get("ok"):
            raise TelegramError(f"getUpdates: {payload.get('description')}")
        return list(payload["result"])

    async def send_media_group(
        self,
        urls: list[str],
        caption: str = "",
        *,
        as_document: bool = False,
        chat_id: str | None = None,
    ) -> list[int]:
        """Галерея из нескольких картинок одним сообщением.

        Возвращает номера сообщений галереи (по одному на картинку); пустой
        список — галерея не ушла.

        `as_document=True` отправляет исходные файлы: Telegram не сжимает их,
        и мелкий текст на экранах остаётся читаемым.

        Кнопок у галереи не бывает: sendMediaGroup не принимает reply_markup.
        Поэтому подпись с кнопкой уходит отдельным сообщением перед галереей.
        """
        if not urls:
            return []
        kind = "document" if as_document else "photo"
        media: list[dict[str, Any]] = []
        for index, url in enumerate(urls[:MEDIA_GROUP_LIMIT]):
            item: dict[str, Any] = {"type": kind, "media": url}
            if index == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        try:
            result = await self._call(
                "sendMediaGroup",
                {"chat_id": chat_id or self.chat_id, "media": json.dumps(media)},
            )
        except TelegramError as exc:
            log.info("sendMediaGroup не прошёл (%s), шлю по одной", exc)
            return []
        ids = [_message_id(item) for item in result] if isinstance(result, list) else []
        return [i for i in ids if i is not None]

    async def copy_message(
        self,
        from_chat_id: str,
        message_id: int,
        *,
        to_chat_id: str,
        caption: str | None = None,
        keyboard: dict[str, Any] | None = None,
    ) -> bool:
        """Повторить уже отправленное сообщение без пометки «переслано».

        Так собирается топ: файл остаётся тем же, качество не теряется, и ни
        Mobbin, ни Anthropic не трогаются. `caption` подменяет подпись — в топе
        она другая: с местом и оценкой.
        """
        data: dict[str, Any] = {
            "chat_id": to_chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if caption is not None:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard)
        try:
            await self._call("copyMessage", data)
            return True
        except TelegramError as exc:
            log.warning("copyMessage %s из %s: %s", message_id, from_chat_id, exc)
            return False

    async def copy_messages(
        self, from_chat_id: str, message_ids: Sequence[int], *, to_chat_id: str
    ) -> bool:
        """То же для галереи: копирует группу целиком, сохраняя группировку."""
        if not message_ids:
            return False
        try:
            await self._call(
                "copyMessages",
                {
                    "chat_id": to_chat_id,
                    "from_chat_id": from_chat_id,
                    "message_ids": json.dumps([int(m) for m in message_ids]),
                },
            )
            return True
        except TelegramError as exc:
            log.warning("copyMessages из %s: %s", from_chat_id, exc)
            return False
