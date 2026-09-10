"""inspobot — ежедневная подборка интерфейсных референсов из Mobbin в Telegram."""

import sys

__version__ = "0.1.0"

MIN_PYTHON = (3, 11)

if sys.version_info < MIN_PYTHON:
    # Без этой проверки первая же ошибка выглядит как «No module named httpx»:
    # pip на старом Python не может поставить anthropic (нужен 3.10+) и молча
    # не ставит ничего, включая httpx. Настоящая причина — версия интерпретатора.
    raise SystemExit(
        "inspobot требует Python {}.{} или новее, а окружение собрано на {}.{}.\n"
        "Зависимость anthropic не ставится на 3.9, поэтому и остальные пакеты "
        "не установились.\n\n"
        "Пересоберите окружение на новом Python:\n"
        "    brew install python@3.12\n"
        "    rm -rf .venv\n"
        '    "$(brew --prefix)/bin/python3.12" -m venv .venv\n'
        "    .venv/bin/pip install -r requirements.txt".format(
            MIN_PYTHON[0], MIN_PYTHON[1], sys.version_info.major, sys.version_info.minor
        )
    )
