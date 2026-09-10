"""Создание клиента Anthropic в одном месте.

Ключ, выпущенный на уровне организации, не привязан к workspace: такой запрос
API отклоняет, пока не придёт заголовок `anthropic-workspace-id`. Ключ,
созданный внутри workspace, заголовка не требует. Поддерживаем оба варианта,
чтобы не заставлять перевыпускать ключ.
"""

from __future__ import annotations

import anthropic

from .config import Config

WORKSPACE_HEADER = "anthropic-workspace-id"


def make_client(config: Config) -> anthropic.Anthropic:
    headers = (
        {WORKSPACE_HEADER: config.anthropic_workspace_id}
        if config.anthropic_workspace_id
        else None
    )
    return anthropic.Anthropic(
        api_key=config.anthropic_api_key or None, default_headers=headers
    )
