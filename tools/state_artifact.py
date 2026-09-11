"""Восстановить базу из последнего артефакта GitHub Actions.

    python tools/state_artifact.py restore var/inspobot.sqlite3

Зачем. Кэш Actions — не хранилище: GitHub чистит его при переполнении и через
неделю без обращений, а топ за месяц опирается на месяц истории. Артефакты
живут заданный срок (здесь 90 дней) и не вычищаются по настроению. Каждый
запуск скачивает свежайший, работает и выкладывает новый.

Коды возврата важны для workflow:
  0 — база восстановлена, или артефакта нет вовсе (первый запуск: начинаем
      с пустой — это нормально);
  1 — артефакт есть, но скачать его не удалось. Работать дальше нельзя:
      пустая база, выложенная в конце запуска, стала бы «свежайшей» и
      затёрла бы всю историю.

Нужны GITHUB_REPOSITORY и GITHUB_TOKEN (стандартный токен запуска с правом
actions: read). Зависимостей нет — только стандартная библиотека.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

API = "https://api.github.com"
DEFAULT_NAME = "inspobot-state"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Скачивание идёт через редирект на хранилище GitHub, и туда нельзя
    пересылать заголовок Authorization — хранилище его отвергает. Поэтому
    редирект ловим сами и второй запрос делаем чистым."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "inspobot-state",
    }


def list_artifacts(repo: str, token: str, name: str) -> list[dict[str, Any]]:
    url = f"{API}/repos/{repo}/actions/artifacts?name={name}&per_page=100"
    req = urllib.request.Request(url, headers=_headers(token))
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read()).get("artifacts", [])


def pick_latest(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Самый свежий из живых. Просроченные GitHub ещё показывает, но скачать
    их уже нельзя."""
    alive = [a for a in artifacts if not a.get("expired")]
    if not alive:
        return None
    return max(alive, key=lambda a: str(a.get("created_at", "")))


def download(url: str, token: str) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with opener.open(req, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = exc.headers.get("Location")
        if not location:
            raise
    clean = urllib.request.Request(location, headers={"User-Agent": "inspobot-state"})
    with urllib.request.urlopen(clean, timeout=120) as response:
        return response.read()


def extract(archive: bytes, target: Path) -> bool:
    """Достать файл базы из zip-архива артефакта. False — файла там нет."""
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for member in zf.namelist():
            if Path(member).name == target.name:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))
                return True
    return False


def restore(target: Path) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    name = os.environ.get("ARTIFACT_NAME", DEFAULT_NAME)
    if not repo or not token:
        print("Нужны GITHUB_REPOSITORY и GITHUB_TOKEN.", file=sys.stderr)
        return 1

    try:
        latest = pick_latest(list_artifacts(repo, token, name))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Не удалось получить список артефактов: {exc}", file=sys.stderr)
        return 1

    if latest is None:
        print(f"Артефакта «{name}» нет — начинаем с пустой базы.")
        return 0

    try:
        archive = download(latest["archive_download_url"], token)
    except (urllib.error.URLError, KeyError) as exc:
        print(
            f"Артефакт {latest.get('id')} от {latest.get('created_at')} есть, "
            f"но скачать не удалось: {exc}. Останавливаюсь, чтобы не затереть историю.",
            file=sys.stderr,
        )
        return 1

    try:
        found = extract(archive, target)
    except zipfile.BadZipFile as exc:
        print(f"Архив артефакта повреждён: {exc}", file=sys.stderr)
        return 1
    if not found:
        print(f"В артефакте нет файла {target.name} — начинаем с пустой базы.")
        return 0

    print(f"База восстановлена из артефакта от {latest.get('created_at')} ({target.stat().st_size} байт).")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "restore":
        print("Использование: state_artifact.py restore ПУТЬ_К_БАЗЕ", file=sys.stderr)
        return 2
    return restore(Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
