#!/usr/bin/env python3
"""Ссылка vless:// → конфиг клиента Xray.

Нужно, чтобы проверить подключение настоящим клиентом с обычного
компьютера, а не сервером к самому себе. Сервер, проходящий через
собственный туннель по петле, не доказывает, что через него пройдёт
кто-то снаружи: ни сеть, ни клиентское приложение при этом не
участвуют.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import sys
import base64
from urllib.parse import parse_qs, unquote, urlparse


class LinkError(Exception):
    """Ссылку разобрать не вышло."""


def _pad(text: str) -> str:
    return text + "=" * (-len(text) % 4)


def parse_ss(link: str) -> dict:
    """Ссылка Shadowsocks по SIP002.

    Пользовательская часть бывает и в base64url, и открытым текстом —
    клиенты выпускают обе, поэтому принимаем обе.
    """
    parsed = urlparse(link.strip())
    if not parsed.hostname:
        raise LinkError("В ссылке нет адреса сервера.")
    userinfo = unquote(parsed.username or "")
    if parsed.password is not None:
        userinfo = f"{userinfo}:{unquote(parsed.password)}"
    elif ":" not in userinfo:
        try:
            userinfo = base64.urlsafe_b64decode(_pad(userinfo)).decode("utf-8")
        except Exception:
            raise LinkError("Пользовательская часть ссылки не разбирается.")
    method, _, password = userinfo.partition(":")
    if not method or not password:
        raise LinkError("В ссылке нет метода или пароля — она обрезана.")
    return {
        "kind": "ss",
        "host": parsed.hostname,
        "port": parsed.port or 443,
        "name": unquote(parsed.fragment) or "клиент",
        "method": method,
        "password": password,
    }


def parse(link: str) -> dict:
    link = link.strip()
    if link.startswith("ss://"):
        return parse_ss(link)
    if not link.startswith("vless://"):
        raise LinkError("Ссылка должна начинаться с vless:// или ss://")
    parsed = urlparse(link)
    if not parsed.username:
        raise LinkError("В ссылке нет идентификатора клиента.")
    if not parsed.hostname:
        raise LinkError("В ссылке нет адреса сервера.")
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    security = query.get("security", "")
    transport = query.get("type", "tcp")
    if security == "reality":
        if not query.get("pbk"):
            raise LinkError("В ссылке нет публичного ключа (pbk) — она обрезана.")
    elif security == "tls":
        # Маршрут через CDN: обычный TLS поверх XHTTP (или старого WebSocket).
        if transport not in ("xhttp", "ws"):
            raise LinkError(f"TLS-ссылка ожидается с type=xhttp или ws, а тут {transport!r}.")
        if not query.get("path"):
            raise LinkError("В ссылке нет пути (path) — она обрезана.")
    else:
        raise LinkError(f"Это не REALITY и не TLS, а {security or 'без защиты'}.")
    return {
        "kind": "vless",
        "id": parsed.username,
        "host": parsed.hostname,
        "port": parsed.port or 443,
        "name": unquote(parsed.fragment) or "клиент",
        "security": security,
        "transport": transport,
        "flow": query.get("flow", ""),
        "sni": query.get("sni", "") or (parsed.hostname if security == "tls" else ""),
        "pbk": query.get("pbk", ""),
        "sid": query.get("sid", ""),
        "fp": query.get("fp", "chrome"),
        "spx": query.get("spx", ""),
        "path": query.get("path", ""),
        "ws_host": query.get("host", "") or parsed.hostname,
        "mode": query.get("mode", ""),
    }


def _socks_inbound(socks_port: int) -> dict:
    return {
        "tag": "socks",
        # Только петля: иначе проверка на минуту открывает
        # наружу незапароленный прокси.
        "listen": "127.0.0.1",
        "port": int(socks_port),
        "protocol": "socks",
        "settings": {"udp": False},
    }


def client_config(link: str, socks_port: int = 10808) -> dict:
    data = parse(link)
    if data["kind"] == "ss":
        return {
            "log": {"loglevel": "warning"},
            "inbounds": [_socks_inbound(socks_port)],
            "outbounds": [
                {
                    "protocol": "shadowsocks",
                    "settings": {
                        "servers": [
                            {
                                "address": data["host"],
                                "port": int(data["port"]),
                                "method": data["method"],
                                "password": data["password"],
                            }
                        ]
                    },
                }
            ],
        }
    user = {"id": data["id"], "encryption": "none"}
    if data["flow"] and data["security"] == "reality":
        user["flow"] = data["flow"]
    if data["security"] == "reality":
        reality = {
            "serverName": data["sni"],
            "fingerprint": data["fp"],
            "publicKey": data["pbk"],
            "shortId": data["sid"],
        }
        if data["spx"]:
            reality["spiderX"] = data["spx"]
        stream = {
            "network": "tcp",
            "security": "reality",
            "realitySettings": reality,
        }
    elif data["transport"] == "xhttp":
        xhttp = {"path": data["path"], "host": data["ws_host"]}
        if data["mode"]:
            xhttp["mode"] = data["mode"]
        stream = {
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {"serverName": data["sni"], "fingerprint": data["fp"]},
            "xhttpSettings": xhttp,
        }
    else:
        stream = {
            "network": "ws",
            "security": "tls",
            "tlsSettings": {"serverName": data["sni"], "fingerprint": data["fp"]},
            "wsSettings": {"path": data["path"], "host": data["ws_host"]},
        }
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [_socks_inbound(socks_port)],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": data["host"],
                            "port": int(data["port"]),
                            "users": [user],
                        }
                    ]
                },
                "streamSettings": stream,
            }
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ссылка vless:// → конфиг клиента")
    parser.add_argument("link")
    parser.add_argument("--socks", type=int, default=10808)
    parser.add_argument("--show", action="store_true", help="разобранные поля, без конфига")
    args = parser.parse_args(argv)
    try:
        if args.show:
            for key, value in parse(args.link).items():
                print(f"{key:<6} {value}")
        else:
            print(json.dumps(client_config(args.link, args.socks), ensure_ascii=False, indent=2))
    except LinkError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
