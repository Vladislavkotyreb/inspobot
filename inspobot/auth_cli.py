"""Вход в Mobbin: `python -m inspobot.auth_cli`.

Два способа.

**На своей машине** (браузер тут же) — одной командой, без аргументов: скрипт
поднимет приёмник, откроет браузер и сам дождётся ответа.

**На сервере** (браузер на другой машине) — в два шага, между которыми ничего
не висит:

    python -m inspobot.auth_cli --start
    python -m inspobot.auth_cli --finish 'http://127.0.0.1:PORT/callback?code=...'

Первая печатает ссылку и запоминает черновик, вторая принимает адрес из
адресной строки браузера и завершает обмен. Браузер при этом покажет ошибку —
так и должно быть: возвращаться ему некуда, а нужные значения уже в адресе.

Результат — var/mobbin_token.json на той машине, где выполнялась команда.
"""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .mobbin_auth import MobbinAuthError, begin_login, complete_login, interactive_login


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Вход в Mobbin")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--start", action="store_true", help="шаг 1: показать ссылку (для сервера)"
    )
    group.add_argument(
        "--finish", metavar="АДРЕС", help="шаг 2: адрес из адресной строки браузера"
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    pending_file = config.mobbin_token_file.with_name("auth_pending.json")

    try:
        if args.start:
            url = begin_login(config.mobbin_mcp_url, pending_file)
            print("1. Откройте эту ссылку в браузере и войдите в Mobbin:\n")
            print(url)
            print(
                "\n2. После входа браузер покажет ошибку — это нормально, "
                "возвращаться ему некуда.\n"
                "   Скопируйте адрес из адресной строки (в нём есть code=) и "
                "выполните:\n\n"
                "   .venv/bin/python -m inspobot.auth_cli --finish 'ВСТАВЬТЕ_АДРЕС'\n\n"
                "   Кавычки обязательны: в адресе есть символ &, без кавычек "
                "оболочка его съест."
            )
            return 0

        if args.finish:
            tokens = complete_login(
                config.mobbin_mcp_url, pending_file, config.mobbin_token_file, args.finish
            )
        else:
            tokens = interactive_login(config.mobbin_mcp_url, config.mobbin_token_file)
    except MobbinAuthError as exc:
        print(f"Не получилось: {exc}", file=sys.stderr)
        return 1

    print(f"\nГотово. Токены сохранены в {config.mobbin_token_file}")
    print(f"Refresh-токен: {'есть' if tokens.refresh_token else 'нет'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
