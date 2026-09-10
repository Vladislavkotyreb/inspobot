"""Список получателей рассылки.

Одна подборка уходит всем: собирается один раз, отправляется в каждый чат.
Список лежит отдельным файлом, а не в `.env`, потому что он растёт — дописать
строку проще, чем править переменную окружения с запятыми.
"""

from __future__ import annotations

from pathlib import Path


def load(path: Path, fallback: str = "") -> tuple[str, ...]:
    """Получатели из файла (по одному id в строке) или единственный из .env.

    Пустые строки и всё после `#` игнорируются — можно подписывать, кто есть кто.
    """
    if not Path(path).exists():
        return (fallback,) if fallback else ()

    found: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line not in found:
            found.append(line)
    if found:
        return tuple(found)
    return (fallback,) if fallback else ()
