#!/usr/bin/env python3
"""Клиенты VLESS + Reality: завести, удалить, показать ссылку.

Единственный источник правды — файл-шпаргалка рядом с конфигом Xray:
там лежат ключи Reality, маскировочный домен и список клиентов. Сам
`config.json` из неё **целиком генерируется заново** при каждом
изменении. Так сделано намеренно: править живой конфиг по месту — это
каждый раз новый шанс уронить рабочий сервер опечаткой в JSON, а
пересборка из шпаргалки либо даёт валидный конфиг, либо падает до
записи.

Публичный ключ хранится только в шпаргалке: в конфиге Xray его нет —
там приватный, — а клиентской ссылке нужен именно публичный.

Только стандартная библиотека: скрипт запускает системный python3
сервера, вне .venv проекта.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import sys
import uuid as uuidlib
from urllib.parse import quote, urlencode

CONFIG = "/usr/local/etc/xray/config.json"
META = "/usr/local/etc/xray/reality.json"
UNIT = "/etc/systemd/system/xray.service"

FLOW = "xtls-rprx-vision"
FINGERPRINT = "chrome"

# Клиент ходит через сервер наружу, и только наружу. Локальные адреса
# закрыты: за ними на этой же машине живут бот, его база и SSH.
# Список задан явными подсетями, а не geoip:private, чтобы конфиг не
# зависел от наличия geoip.dat — одним поводом для падения меньше.
PRIVATE_NETS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


class VlessError(Exception):
    """Ошибка, которую надо показать человеку, а не трассировкой."""


# --- шпаргалка -------------------------------------------------------


def load_meta(path: str = META) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise VlessError(
            f"Нет файла {path} — сервер ещё не установлен.\n"
            "Запустите: sudo sh deploy/vless.sh install"
        )
    except json.JSONDecodeError as error:
        raise VlessError(f"{path} не читается как JSON: {error}")


def save_meta(meta: dict, path: str = META) -> None:
    # Шпаргалку читает только root: Xray в неё не заглядывает.
    _write(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n", 0o600)


def _write(path: str, text: str, mode: int) -> None:
    """Запись через временный файл: до конца записи старый файл цел."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o755, exist_ok=True)
    temporary = path + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def service_gid(unit_path: str = UNIT) -> int | None:
    """Группа, под которой systemd запускает Xray.

    Штатный установщик поднимает демона от `nobody`, а не от root.
    Конфиг в режиме 0600 от root такой демон открыть не может и падает
    с `permission denied`, ничего про права не сказав. Поэтому группу
    берём из юнита, а не предполагаем.
    """
    user, group = "nobody", ""
    try:
        with open(unit_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("User="):
                    user = line.split("=", 1)[1].strip() or user
                elif line.startswith("Group="):
                    group = line.split("=", 1)[1].strip()
    except OSError:
        return None
    try:
        if group:
            return grp.getgrnam(group).gr_gid
        return pwd.getpwnam(user).pw_gid
    except KeyError:
        return None


# --- конфиг Xray -----------------------------------------------------


def render_config(meta: dict) -> dict:
    for key in ("port", "sni", "dest", "private_key", "short_id"):
        if not meta.get(key):
            raise VlessError(f"В шпаргалке нет поля {key!r} — конфиг собрать не из чего.")
    clients = [
        {"id": client["id"], "flow": FLOW, "email": client["name"]}
        for client in meta.get("clients", [])
    ]
    return {
        # access: none — на диске не копится, кто куда ходил. Это и про
        # приватность, и про место: журнал посещений растёт быстро.
        "log": {"loglevel": "warning", "access": "none"},
        "inbounds": [
            {
                "tag": "vless-reality",
                "listen": "0.0.0.0",
                "port": int(meta["port"]),
                "protocol": "vless",
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": meta["dest"],
                        "xver": 0,
                        "serverNames": [meta["sni"]],
                        "privateKey": meta["private_key"],
                        "shortIds": [meta["short_id"]],
                    },
                },
                # routeOnly: распознанный домен идёт только в правила
                # маршрутизации, адрес соединения остаётся исходным.
                # Без этого ломаются подключения по голому IP.
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": True,
                },
            }
        ],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "ip": list(PRIVATE_NETS), "outboundTag": "block"},
                # Торренты с этого адреса — жалоба хостеру, а адрес общий
                # с ботом: заблокируют машину, встанет и рассылка.
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
            ],
        },
    }


def write_config(meta: dict, path: str = CONFIG, unit_path: str = UNIT) -> None:
    text = json.dumps(render_config(meta), ensure_ascii=False, indent=2) + "\n"
    _write(path, text, 0o600)
    gid = service_gid(unit_path)
    if gid is None:
        return
    # 0640 root:<группа демона> — Xray читает, посторонние нет. Штатный
    # установщик кладёт конфиг с приватным ключом в 0644, то есть
    # доступным всем; здесь строже.
    try:
        os.chown(path, 0, gid)
        os.chmod(path, 0o640)
    except OSError:
        # Не root или чужая файловая система: файл остаётся 0600.
        # Служба тогда не поднимется, но это видно в журнале, а молча
        # раздавать ключ всем подряд — хуже.
        return
    directory = os.path.dirname(path) or "."
    try:
        # В каталог демону нужно хотя бы войти. Секрет здесь же, в
        # шпаргалке, но она 0600 — открытый каталог её не выдаёт.
        os.chmod(directory, 0o755)
    except OSError:
        pass


# --- клиенты ---------------------------------------------------------


def check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise VlessError(
            f"Имя {name!r} не годится. Латиница, цифры, точка, дефис, "
            "подчёркивание; до 32 знаков. Например: vlad-iphone"
        )
    return name


def add_client(meta: dict, name: str, client_id: str | None = None) -> dict:
    check_name(name)
    if any(client["name"] == name for client in meta.get("clients", [])):
        raise VlessError(f"Клиент {name!r} уже есть. Ссылка: vless.sh link {name}")
    client = {"name": name, "id": client_id or str(uuidlib.uuid4())}
    meta.setdefault("clients", []).append(client)
    return client


def remove_client(meta: dict, name: str) -> dict:
    clients = meta.get("clients", [])
    for index, client in enumerate(clients):
        if client["name"] == name:
            return clients.pop(index)
    raise VlessError(f"Клиента {name!r} нет. Список: vless.sh list")


def find_client(meta: dict, name: str) -> dict:
    for client in meta.get("clients", []):
        if client["name"] == name:
            return client
    raise VlessError(f"Клиента {name!r} нет. Список: vless.sh list")


# --- ссылка ----------------------------------------------------------


def link(meta: dict, client: dict) -> str:
    host = meta["host"]
    if ":" in host:  # IPv6 в URL берётся в квадратные скобки
        host = f"[{host}]"
    params = {
        "type": "tcp",
        "security": "reality",
        "encryption": "none",
        "flow": FLOW,
        "pbk": meta["public_key"],
        "fp": FINGERPRINT,
        "sni": meta["sni"],
        "sid": meta["short_id"],
        "spx": "/",
    }
    query = urlencode(params, quote_via=quote, safe="")
    return f"vless://{client['id']}@{host}:{meta['port']}?{query}#{quote(client['name'])}"


# --- командная строка ------------------------------------------------


def _apply(meta: dict, config_path: str, meta_path: str) -> None:
    """Сначала конфиг, потом шпаргалка: если конфиг не собрался, на
    диске остаётся прежняя пара, а не расползшаяся."""
    write_config(meta, config_path)
    save_meta(meta, meta_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Клиенты VLESS + Reality")
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--meta", default=META)
    commands = parser.add_subparsers(dest="command", required=True)

    initial = commands.add_parser("init", help="создать шпаргалку и конфиг")
    for option in ("host", "port", "sni", "dest", "private-key", "public-key", "short-id"):
        initial.add_argument("--" + option, required=True)
    initial.add_argument("--client", required=True)

    for name, help_text in (
        ("add", "завести клиента"),
        ("remove", "удалить клиента"),
        ("link", "показать ссылку"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("name")

    commands.add_parser("list", help="все клиенты со ссылками")
    commands.add_parser("render", help="пересобрать конфиг из шпаргалки")
    commands.add_parser("show", help="параметры сервера")
    field = commands.add_parser("get", help="одно поле шпаргалки")
    field.add_argument("field")

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            meta = {
                "host": args.host,
                "port": int(args.port),
                "sni": args.sni,
                "dest": args.dest,
                "private_key": getattr(args, "private_key"),
                "public_key": getattr(args, "public_key"),
                "short_id": getattr(args, "short_id"),
                "clients": [],
            }
            client = add_client(meta, args.client)
            _apply(meta, args.config, args.meta)
            print(link(meta, client))
            return 0

        meta = load_meta(args.meta)

        if args.command == "add":
            client = add_client(meta, args.name)
            _apply(meta, args.config, args.meta)
            print(link(meta, client))
        elif args.command == "remove":
            remove_client(meta, args.name)
            _apply(meta, args.config, args.meta)
            print(f"Удалён: {args.name}")
        elif args.command == "link":
            print(link(meta, find_client(meta, args.name)))
        elif args.command == "list":
            clients = meta.get("clients", [])
            if not clients:
                print("Клиентов нет. Завести: vless.sh add имя")
            for client in clients:
                print(f"{client['name']}\n{link(meta, client)}\n")
        elif args.command == "render":
            write_config(meta, args.config)
            print(f"Конфиг пересобран: {args.config}")
        elif args.command == "get":
            value = meta.get(args.field)
            if value is None:
                raise VlessError(f"В шпаргалке нет поля {args.field!r}.")
            print(value)
        elif args.command == "show":
            print(f"адрес:  {meta['host']}:{meta['port']}")
            print(f"маска:  {meta['sni']}")
            print(f"клиентов: {len(meta.get('clients', []))}")
    except VlessError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
