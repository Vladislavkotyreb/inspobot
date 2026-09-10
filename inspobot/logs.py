"""Настройка логов.

Отдельный модуль по одной причине: `--verbose` не должен включать отладку
чужих библиотек. SDK anthropic на уровне DEBUG печатает тело запроса целиком,
а в теле лежит `authorization_token` — access-токен Mobbin. Один такой лог,
отправленный в чат или в issue, отдаёт доступ к аккаунту.

Поэтому подробность повышается только для логгера `inspobot`, а сетевые
библиотеки прижаты к WARNING независимо от флага.
"""

from __future__ import annotations

import logging

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Всё, что умеет печатать тело HTTP-запроса или заголовки.
NOISY = ("anthropic", "anthropic._base_client", "httpx", "httpx2", "httpcore", "httpcore2")


def setup(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format=FORMAT)
    logging.getLogger("inspobot").setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
