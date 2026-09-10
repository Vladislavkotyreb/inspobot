"""Записать значение в секрет репозитория GitHub.

Нужно из-за того, как устроен доступ к Mobbin. Их авторизация построена на
Supabase, а тот при каждом обновлении выдаёт новый refresh-токен и гасит
старый. Раннер GitHub Actions живёт один запуск и ничего не помнит, поэтому
обновлённый токен надо положить обратно в секрет — иначе завтрашний запуск
останется без доступа.

Запускается только в CI, в зависимости проекта не входит:

    python tools/update_github_secret.py MOBBIN_TOKEN_JSON var/mobbin_token.json

Требует переменных GITHUB_REPOSITORY и GH_SECRETS_TOKEN (токен с правом
записи секретов этого репозитория).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from nacl import encoding, public

API = "https://api.github.com"


def request(url: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def seal(public_key: str, value: str) -> str:
    """Секреты принимаются только зашифрованными публичным ключом репозитория."""
    key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    return base64.b64encode(public.SealedBox(key).encrypt(value.encode("utf-8"))).decode("utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Использование: update_github_secret.py ИМЯ_СЕКРЕТА ФАЙЛ", file=sys.stderr)
        return 2
    name, path = argv

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_SECRETS_TOKEN", "")
    if not repo or not token:
        print("Нужны GITHUB_REPOSITORY и GH_SECRETS_TOKEN.", file=sys.stderr)
        return 1

    value = Path(path).read_text(encoding="utf-8")
    try:
        key = request(f"{API}/repos/{repo}/actions/secrets/public-key", token)
        request(
            f"{API}/repos/{repo}/actions/secrets/{name}",
            token,
            method="PUT",
            payload={"encrypted_value": seal(key["key"], value), "key_id": key["key_id"]},
        )
    except urllib.error.HTTPError as exc:
        # Тело ошибки печатаем: в нём причина, а сам секрет туда не попадает.
        print(f"GitHub ответил {exc.code}: {exc.read()[:300]!r}", file=sys.stderr)
        return 1

    print(f"Секрет {name} обновлён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
