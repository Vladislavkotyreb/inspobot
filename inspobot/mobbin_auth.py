"""OAuth-доступ к Mobbin MCP.

У Mobbin нет API-ключа для MCP: сервер удалённый и авторизуется по OAuth 2.1
(discovery → dynamic client registration → authorization code + PKCE), как это
описано в спецификации MCP. Поэтому здесь два режима:

* ``python -m inspobot.auth_cli`` — один раз, на машине с браузером: проходим
  вход в Mobbin и сохраняем refresh-токен в JSON;
* ``get_access_token()`` — на сервере: молча обновляет access-токен по
  refresh-токену перед каждым обращением.

Файл с токенами кладётся с правами 0600 и в git не попадает.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

CLIENT_NAME = "inspobot"
USER_AGENT = "inspobot/0.1 (+https://github.com/Vladislavkotyreb/inspobot)"
CALLBACK_HOST = "127.0.0.1"
REFRESH_MARGIN_SECONDS = 120


class MobbinAuthError(RuntimeError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    client_id: str
    client_secret: str
    token_endpoint: str
    scope: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - REFRESH_MARGIN_SECONDS


# --- хранение ---------------------------------------------------------------


def load_tokens(path: Path) -> Tokens | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Tokens(**data)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MobbinAuthError(f"Файл {path} повреждён: {exc}") from exc


def save_tokens(path: Path, tokens: Tokens) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(tokens), indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


# --- discovery --------------------------------------------------------------


def _split(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")


def _get_json(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = client.get(url, headers={"MCP-Protocol-Version": "2025-06-18"})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _resource_metadata_url(client: httpx.Client, mcp_url: str) -> str | None:
    """Спросить сам MCP-эндпоинт, где лежат метаданные (RFC 9728)."""
    try:
        response = client.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    except httpx.HTTPError:
        return None
    if response.status_code not in (401, 403):
        return None
    header = response.headers.get("www-authenticate", "")
    match = re.search(r'resource_metadata="?([^",]+)"?', header)
    return match.group(1) if match else None


def discover(client: httpx.Client, mcp_url: str) -> dict[str, Any]:
    """Метаданные authorization server для данного MCP-эндпоинта."""
    origin, path = _split(mcp_url)

    prm_urls = [u for u in (_resource_metadata_url(client, mcp_url),) if u]
    prm_urls += [
        f"{origin}/.well-known/oauth-protected-resource{path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ]

    issuers: list[str] = []
    for url in prm_urls:
        prm = _get_json(client, url)
        if prm and prm.get("authorization_servers"):
            issuers = [str(i) for i in prm["authorization_servers"]]
            break
    if not issuers:
        # Сервер не отдал protected-resource metadata — пробуем сам origin.
        issuers = [origin]

    for issuer in issuers:
        iss_origin, iss_path = _split(issuer)
        for url in (
            f"{iss_origin}/.well-known/oauth-authorization-server{iss_path}",
            f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
            f"{iss_origin}/.well-known/oauth-authorization-server",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        ):
            meta = _get_json(client, url)
            if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                return meta

    raise MobbinAuthError(
        f"Не удалось найти OAuth-метаданные для {mcp_url}. "
        "Проверьте MOBBIN_MCP_URL или задайте MOBBIN_ACCESS_TOKEN вручную."
    )


# --- регистрация клиента ----------------------------------------------------


def register_client(
    client: httpx.Client, meta: dict[str, Any], redirect_uri: str, scope: str
) -> tuple[str, str]:
    preset = os.environ.get("MOBBIN_CLIENT_ID", "").strip()
    if preset:
        return preset, os.environ.get("MOBBIN_CLIENT_SECRET", "").strip()

    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise MobbinAuthError(
            "Сервер не поддерживает динамическую регистрацию клиента. "
            "Заведите OAuth-приложение вручную и задайте MOBBIN_CLIENT_ID."
        )

    payload: dict[str, Any] = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }
    if scope:
        payload["scope"] = scope

    response = client.post(endpoint, json=payload)
    if response.status_code >= 400:
        raise MobbinAuthError(
            f"Регистрация клиента отклонена ({response.status_code}): {response.text[:400]}"
        )
    data = response.json()
    return str(data["client_id"]), str(data.get("client_secret", ""))


# --- authorization code + PKCE ---------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        query = urllib.parse.urlparse(self.path).query
        _CallbackHandler.result = {
            k: v[0] for k, v in urllib.parse.parse_qs(query).items()
        }
        body = (
            "<html><meta charset='utf-8'><body style='font:16px system-ui;padding:40px'>"
            "<h2>Готово</h2><p>Можно закрыть вкладку и вернуться в терминал.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # тише в терминале
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((CALLBACK_HOST, 0))
        return int(sock.getsockname()[1])


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _token_request(
    client: httpx.Client,
    meta_or_endpoint: str,
    form: dict[str, str],
    client_secret: str,
) -> dict[str, Any]:
    if client_secret:
        form = {**form, "client_secret": client_secret}
    response = client.post(
        meta_or_endpoint,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        raise _token_error(response.status_code, response.text)
    return response.json()


# Признаки того, что refresh-токен мёртв окончательно и повторять бессмысленно.
SPENT_TOKEN_MARKERS = (
    "refresh_token_already_used",
    "refresh_token_not_found",
    "invalid_grant",
)

SPENT_TOKEN_HELP = (
    "Refresh-токен Mobbin больше не действует.\n\n"
    "Их авторизация на Supabase: при каждом обновлении выдаётся новый "
    "refresh-токен, а старый гасится. Этот уже был использован — почти всегда "
    "это значит, что одним и тем же файлом пользуются в двух местах сразу "
    "(например, сервер и GitHub Actions): кто обновился первым, тот и оставил "
    "второго ни с чем.\n\n"
    "Лечится входом заново:\n"
    "    python -m inspobot.auth_cli\n\n"
    "И проследите, чтобы дайджест запускался только в одном месте."
)


def _token_error(status: int, body: str) -> MobbinAuthError:
    if any(marker in body for marker in SPENT_TOKEN_MARKERS):
        return MobbinAuthError(SPENT_TOKEN_HELP)
    return MobbinAuthError(f"Token endpoint вернул {status}: {body[:400]}")


def _tokens_from_response(
    data: dict[str, Any],
    *,
    client_id: str,
    client_secret: str,
    token_endpoint: str,
    fallback_refresh: str = "",
) -> Tokens:
    return Tokens(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token") or fallback_refresh),
        expires_at=time.time() + float(data.get("expires_in", 3600)),
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint=token_endpoint,
        scope=str(data.get("scope", "")),
    )


def interactive_login(mcp_url: str, token_file: Path, timeout: int = 300) -> Tokens:
    """Полный вход в Mobbin через браузер. Запускать на своей машине."""
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        meta = discover(client, mcp_url)
        scope = " ".join(meta.get("scopes_supported", []) or [])
        port = _free_port()
        redirect_uri = f"http://{CALLBACK_HOST}:{port}/callback"
        client_id, client_secret = register_client(client, meta, redirect_uri, scope)

        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": mcp_url,
        }
        if scope:
            params["scope"] = scope
        auth_url = f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(params)}"

        server = http.server.HTTPServer((CALLBACK_HOST, port), _CallbackHandler)
        server.timeout = timeout
        _CallbackHandler.result = {}
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        print("Откройте ссылку и войдите в Mobbin:\n")
        print(auth_url + "\n")
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001 — на сервере браузера просто нет
            pass

        thread.join(timeout)
        server.server_close()
        result = _CallbackHandler.result
        if not result:
            raise MobbinAuthError("Ответ от Mobbin не пришёл: истекло время ожидания.")
        if "error" in result:
            raise MobbinAuthError(
                f"Mobbin отказал: {result['error']} {result.get('error_description', '')}"
            )
        if result.get("state") != state:
            raise MobbinAuthError("Не совпал state — ответ пришёл не на наш запрос.")

        data = _token_request(
            client,
            meta["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": mcp_url,
            },
            client_secret,
        )
        tokens = _tokens_from_response(
            data,
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint=meta["token_endpoint"],
        )
        if not tokens.refresh_token:
            print(
                "Внимание: сервер не выдал refresh-токен — доступ придётся "
                "обновлять вручную, когда access-токен истечёт."
            )
        save_tokens(token_file, tokens)
        return tokens


def refresh(tokens: Tokens, mcp_url: str) -> Tokens:
    if not tokens.refresh_token:
        raise MobbinAuthError(
            "Access-токен истёк, а refresh-токена нет. "
            "Запустите `python -m inspobot.auth_cli` заново."
        )
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        data = _token_request(
            client,
            tokens.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": tokens.client_id,
                "resource": mcp_url,
            },
            tokens.client_secret,
        )
    return _tokens_from_response(
        data,
        client_id=tokens.client_id,
        client_secret=tokens.client_secret,
        token_endpoint=tokens.token_endpoint,
        fallback_refresh=tokens.refresh_token,
    )


def get_access_token(
    token_file: Path, mcp_url: str, override: str = ""
) -> str:
    """Токен для поля ``authorization_token`` MCP-коннектора Claude API."""
    if override:
        return override
    tokens = load_tokens(token_file)
    if tokens is None:
        raise MobbinAuthError(
            f"Нет файла с токенами Mobbin ({token_file}). "
            "Выполните на своей машине `python -m inspobot.auth_cli` и скопируйте файл на сервер."
        )
    if tokens.expired:
        tokens = refresh(tokens, mcp_url)
        save_tokens(token_file, tokens)
    return tokens.access_token
