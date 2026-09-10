"""Вызов Messages API с MCP-коннектором Mobbin.

Claude сам подключается к удалённому MCP-серверу Mobbin (соединение держит
сторона Anthropic), вызывает его инструменты, смотрит на картинки экранов и
возвращает JSON по схеме из prompt.py. Клиентского цикла tool_use здесь нет —
он не нужен для серверных MCP-инструментов, кроме продолжения по pause_turn.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Sequence

import anthropic

from .client import make_client
from .config import Config
from .catalog import Topic
from .models import Digest
from .profile import Slot
from .prompt import (
    DIGEST_SCHEMA,
    SYSTEM_PROMPT,
    CuratorError,
    build_digest_messages,
    parse_digest,
)

MCP_BETA = "mcp-client-2025-11-20"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SERVER_NAME = "mobbin"
MAX_TURNS = 6

log = logging.getLogger(__name__)


def _final_text(message: Any) -> str:
    for block in message.content:
        if getattr(block, "type", None) == "text" and getattr(block, "text", "").strip():
            return block.text
    raise CuratorError("Модель не вернула текстовый блок с JSON.")


def build_request(config: Config, mobbin_token: str) -> dict[str, Any]:
    betas = [MCP_BETA] + ([FALLBACK_BETA] if config.server_fallbacks else [])
    request: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": SYSTEM_PROMPT,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": DIGEST_SCHEMA},
        },
        "mcp_servers": [
            {
                "type": "url",
                "url": config.mobbin_mcp_url,
                "name": SERVER_NAME,
                "authorization_token": mobbin_token,
            }
        ],
        "tools": [{"type": "mcp_toolset", "mcp_server_name": SERVER_NAME}],
        "betas": betas,
    }
    if config.server_fallbacks:
        request["fallbacks"] = "default"
    return request


def drop_fallbacks(request: dict[str, Any], error_text: str) -> bool:
    """Аккаунт без беты server-side fallback — повторить без неё, а не упасть."""
    if "fallbacks" not in request or "fallback" not in error_text.lower():
        return False
    request.pop("fallbacks", None)
    request["betas"] = [b for b in request["betas"] if b != FALLBACK_BETA]
    return True


def _run(client: anthropic.Anthropic, request: dict[str, Any], messages: list[dict[str, Any]]) -> Any:
    for _ in range(MAX_TURNS):
        try:
            with client.beta.messages.stream(**request, messages=messages) as stream:
                message = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            if drop_fallbacks(request, str(exc)):
                log.info("Повторяю запрос без server-side fallback")
                continue
            raise
        if message.stop_reason == "refusal":
            raise CuratorError(f"Модель отклонила запрос: {message.stop_details}")
        if message.stop_reason != "pause_turn":
            return message
        messages = messages + [{"role": "assistant", "content": message.content}]
    raise CuratorError("Слишком много продолжений подряд — прерываю.")


def collect(
    config: Config,
    day: date,
    plan: Sequence[tuple[Slot, Topic]],
    mobbin_token: str,
    seen_by_platform: dict[str, Sequence[str]] | None = None,
    title: str = "",
) -> Digest:
    """Один запрос к Messages API собирает весь дайджест целиком."""
    client = make_client(config)
    messages = build_digest_messages(day, plan, seen_by_platform)
    message = _run(client, build_request(config, mobbin_token), messages)
    log.info(
        "Токены: вход %s, выход %s",
        getattr(message.usage, "input_tokens", "?"),
        getattr(message.usage, "output_tokens", "?"),
    )
    seen: set[str] = set()
    for ids in (seen_by_platform or {}).values():
        seen.update(ids)
    return parse_digest(_final_text(message), day, plan, seen, title)
