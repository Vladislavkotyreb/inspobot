"""Отправка в Telegram напрямую по Bot API — без фреймворка.

Из зависимостей только httpx: на shared-хостинге чем меньше пакетов, тем
меньше поводов для сюрпризов при обновлении Python.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

API_ROOT = "https://api.telegram.org"
SEND_PAUSE = 0.6  # Telegram: не больше ~20 сообщений в минуту в один чат
MEDIA_GROUP_LIMIT = 10  # столько картинок влезает в одну галерею
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


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
        response = await self._client.post(self._url(method), data=data, files=files)
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
    ) -> bool:
        """Одна картинка: файлом или фотографией, с кнопками под ней.

        `as_document=True` шлёт исходный файл — Telegram его не сжимает, и
        мелкий текст на экране остаётся читаемым.

        Сначала пробуем отдать ссылку Telegram, потом качаем сами. Возвращает
        False, если доставить не вышло, — вызывающий код тогда шлёт тот же
        текст без изображения.
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
            await self._call(method, {**base, field: image_url})
            return True
        except TelegramError as exc:
            log.info("%s по ссылке не прошёл (%s), качаю сам", method, exc)

        try:
            blob = await self._client.get(image_url, follow_redirects=True)
            blob.raise_for_status()
            content_type = blob.headers.get("content-type", "image/jpeg")
            suffix = "webp" if "webp" in content_type else "jpg"
            await self._call(
                method,
                base,
                files={field: (f"screen.{suffix}", blob.content, content_type)},
            )
            return True
        except (httpx.HTTPError, TelegramError) as exc:
            log.warning("Не удалось отправить %s: %s", image_url, exc)
            return False

    async def pause(self) -> None:
        await asyncio.sleep(SEND_PAUSE)

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        response = await self._client.post(
            self._url("getUpdates"),
            data={"offset": offset, "timeout": timeout},
            timeout=httpx.Timeout(timeout + 20.0, connect=15.0),
        )
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
    ) -> bool:
        """Галерея из нескольких картинок одним сообщением.

        `as_document=True` отправляет исходные файлы: Telegram не сжимает их,
        и мелкий текст на экранах остаётся читаемым.

        Кнопок у галереи не бывает: sendMediaGroup не принимает reply_markup.
        Поэтому подпись с кнопкой уходит отдельным сообщением перед галереей.
        """
        if not urls:
            return False
        kind = "document" if as_document else "photo"
        media: list[dict[str, Any]] = []
        for index, url in enumerate(urls[:MEDIA_GROUP_LIMIT]):
            item: dict[str, Any] = {"type": kind, "media": url}
            if index == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        try:
            await self._call(
                "sendMediaGroup",
                {"chat_id": chat_id or self.chat_id, "media": json.dumps(media)},
            )
            return True
        except TelegramError as exc:
            log.info("sendMediaGroup не прошёл (%s), шлю по одной", exc)
            return False
