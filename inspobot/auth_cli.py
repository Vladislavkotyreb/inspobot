"""Одноразовый вход в Mobbin: `python -m inspobot.auth_cli`.

Запускать на машине с браузером. Результат — файл var/mobbin_token.json,
который нужно скопировать на сервер (права 0600, в git не попадает).
"""

from __future__ import annotations

import sys

from .config import Config
from .mobbin_auth import MobbinAuthError, interactive_login


def main() -> int:
    config = Config.from_env()
    try:
        tokens = interactive_login(config.mobbin_mcp_url, config.mobbin_token_file)
    except MobbinAuthError as exc:
        print(f"Не получилось: {exc}", file=sys.stderr)
        return 1
    print(f"\nГотово. Токены сохранены в {config.mobbin_token_file}")
    print(f"Refresh-токен: {'есть' if tokens.refresh_token else 'нет'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
