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
import base64
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
# Отпечаток задаёт форму ClientHello, и по нему могут резать: ТСПУ
# отбрасывают одни отпечатки и пропускают другие. Это свойство ссылки,
# а не сервера — серверу всё равно, кем прикидывается клиент.
# 2022-blake3 — единственное семейство Shadowsocks без известных
# способов опознания по трафику; older-методы (aes-256-gcm и прочие)
# DPI распознаёт.
SS_METHOD = "2022-blake3-aes-128-gcm"
SS_KEY_BYTES = 16

FINGERPRINTS = (
    "chrome", "firefox", "safari", "ios", "android", "edge",
    "360", "qq", "random", "randomized", "randomizednoalpn",
)

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


def _inbound(
    meta: dict, tag: str, port: int, sni: str, clients: list, flow: str = FLOW
) -> dict:
    """Один вход REALITY. Ключи и клиенты общие у всех входов: меняется
    только порт, домен, за которым вход прячется, и наличие Vision."""
    if flow != FLOW:
        clients = [{**client, "flow": flow} if flow else
                   {key: value for key, value in client.items() if key != "flow"}
                   for client in clients]
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": int(port),
        "protocol": "vless",
        "settings": {"clients": clients, "decryption": "none"},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": f"{sni}:443",
                "xver": 0,
                "serverNames": [sni],
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


def _cdn_inbound(meta: dict, clients: list) -> dict:
    """Вход для маршрута через CDN.

    Когда DPI режет любой TLS к нашему адресу, единственный способ до
    него добраться — не ходить на него вовсе. Человек подключается к
    адресу Cloudflare (для DPI это обычный сайт за CDN), а Cloudflare
    сам ходит сюда. REALITY через CDN не проходит — он подменяет TLS,
    а CDN его терминирует; поэтому здесь обычный TLS поверх XHTTP.
    XHTTP выбран вместо WebSocket не по вкусу: Xray 26 объявил WebSocket
    устаревшим и прямо рекомендует XHTTP, а тот и задуман для CDN —
    режим packet-up отправляет данные обычными HTTP-запросами, которые
    Cloudflare пропускает как есть. Vision с этим несовместим: flow у
    клиентов снимаем.
    """
    cdn = meta["cdn"]
    plain = [{k: v for k, v in c.items() if k != "flow"} for c in clients]
    return {
        "tag": "vless-cdn",
        "listen": "0.0.0.0",
        "port": int(cdn.get("port", 443)),
        "protocol": "vless",
        "settings": {"clients": plain, "decryption": "none"},
        "streamSettings": {
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {
                "certificates": [
                    {"certificateFile": cdn["cert"], "keyFile": cdn["key"]}
                ],
            },
            "xhttpSettings": {"path": cdn["path"]},
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
            "routeOnly": True,
        },
    }


def _ss_inbound(meta: dict) -> dict:
    """Вход Shadowsocks-2022.

    Нужен там, где DPI убивает рукопожатие TLS: у Shadowsocks его нет
    вовсе. На проводе — поток случайных на вид байт с первого байта,
    опознавать нечего, инспектировать нечего. Шифрование при этом
    полноценное, в отличие от «просто без TLS».

    Ключи у него свои: протокол другой, клиенты VLESS сюда не ходят.
    """
    ss = meta["ss"]
    return {
        "tag": "shadowsocks",
        "listen": "0.0.0.0",
        "port": int(ss["port"]),
        "protocol": "shadowsocks",
        "settings": {
            "method": ss["method"],
            "password": ss["password"],
            "network": "tcp,udp",
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
            "routeOnly": True,
        },
    }


def render_config(meta: dict, loglevel: str = "warning") -> dict:
    for key in ("port", "sni", "dest", "private_key", "short_id"):
        if not meta.get(key):
            raise VlessError(f"В шпаргалке нет поля {key!r} — конфиг собрать не из чего.")
    clients = [
        {"id": client["id"], "flow": FLOW, "email": client["name"]}
        for client in meta.get("clients", [])
    ]
    # Основной вход плюс пробные: они нужны, чтобы человек на той
    # стороне перебрал домены сам, импортировав несколько ссылок, а не
    # ждал круга переписки на каждый.
    inbounds = [_inbound(meta, "vless-reality", meta["port"], meta["sni"], clients)]
    for index, alt in enumerate(meta.get("alts", []), start=1):
        inbounds.append(
            _inbound(
                meta, f"vless-proba-{index}", alt["port"], alt["sni"], clients,
                alt.get("flow", FLOW),
            )
        )
    if meta.get("cdn"):
        inbounds.append(_cdn_inbound(meta, clients))
    if meta.get("ss"):
        inbounds.append(_ss_inbound(meta))
    return {
        # access: none — на диске не копится, кто куда ходил. Это и про
        # приватность, и про место: журнал посещений растёт быстро.
        "log": {"loglevel": loglevel, "access": "none"},
        "inbounds": inbounds,
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


def write_config(
    meta: dict,
    path: str = CONFIG,
    unit_path: str = UNIT,
    loglevel: str = "warning",
) -> None:
    text = json.dumps(render_config(meta, loglevel), ensure_ascii=False, indent=2) + "\n"
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


def link(meta: dict, client: dict, alt: dict | None = None, label_suffix: str = "") -> str:
    host = meta["host"]
    if ":" in host:  # IPv6 в URL берётся в квадратные скобки
        host = f"[{host}]"
    port = alt["port"] if alt else meta["port"]
    sni = alt["sni"] if alt else meta["sni"]
    flow = alt.get("flow", FLOW) if alt else FLOW
    label = client["name"]
    if alt:
        label = f"{client['name']}-{sni}" + ("" if flow else "-novision")
    label = label + label_suffix
    params = {
        "type": "tcp",
        "security": "reality",
        "encryption": "none",
        "flow": flow,
        "pbk": meta["public_key"],
        "fp": meta.get("fingerprint") or FINGERPRINT,
        "sni": sni,
        "sid": meta["short_id"],
        "spx": "/",
    }
    params = {key: value for key, value in params.items() if value != ""}
    query = urlencode(params, quote_via=quote, safe="")
    return f"vless://{client['id']}@{host}:{port}?{query}#{quote(label)}"


def client_config(
    meta: dict,
    client: dict,
    socks_port: int = 10808,
    address: str | None = None,
) -> dict:
    """Конфиг клиента для самопроверки.

    Тот же туннель, которым пойдёт телефон, только поднятый на самом
    сервере и выведенный в локальный socks. Если через него ходит
    трафик — связка «сервер + ссылка» рабочая, и остаётся один
    подозреваемый: приложение.

    `address` подменяет адрес сервера, не трогая SNI: Reality смотрит
    на имя, а не на адрес. Через 127.0.0.1 проверяется рукопожатие
    само по себе, без сети хостера, — та часто не пускает сервер на
    его же внешний адрес, и проверка врёт про поломку.
    """
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks",
                "listen": "127.0.0.1",
                "port": int(socks_port),
                "protocol": "socks",
                "settings": {"udp": False},
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": address or meta["host"],
                            "port": int(meta["port"]),
                            "users": [
                                {
                                    "id": client["id"],
                                    "encryption": "none",
                                    "flow": FLOW,
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "serverName": meta["sni"],
                        "fingerprint": meta.get("fingerprint") or FINGERPRINT,
                        "publicKey": meta["public_key"],
                        "shortId": meta["short_id"],
                        "spiderX": "/",
                    },
                },
            }
        ],
    }


def cdn_link(meta: dict, client: dict) -> str:
    """Ссылка на вход через CDN: адрес — домен, а не наш IP."""
    cdn = meta["cdn"]
    domain = cdn["domain"]
    params = {
        "type": "xhttp",
        "security": "tls",
        "encryption": "none",
        "sni": domain,
        "host": domain,
        "path": cdn["path"],
        # packet-up — режим для CDN: загрузка отдельными POST, скачивание
        # одним потоком. stream-up через Cloudflare не проходит.
        "mode": "packet-up",
        "fp": meta.get("fingerprint") or FINGERPRINT,
    }
    query = urlencode(params, quote_via=quote, safe="")
    label = quote(client["name"] + "-cdn")
    return f"vless://{client['id']}@{domain}:{cdn.get('port', 443)}?{query}#{label}"


def ss_link(meta: dict) -> str:
    """Ссылка Shadowsocks по SIP002.

    Часть клиентов ждёт пользовательскую часть в base64url, часть —
    открытым текстом. base64url — исходный вариант SIP002 и понимается
    шире, поэтому он.
    """
    ss = meta["ss"]
    host = meta["host"]
    if ":" in host:
        host = f"[{host}]"
    userinfo = f"{ss['method']}:{ss['password']}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(userinfo).decode("ascii").rstrip("=")
    label = quote(f"{ss.get('name', 'ss')}")
    return f"ss://{encoded}@{host}:{ss['port']}#{label}"


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
    rerender = commands.add_parser("render", help="пересобрать конфиг из шпаргалки")
    rerender.add_argument("--loglevel", default="warning",
                          choices=["none", "error", "warning", "info", "debug"])
    commands.add_parser("show", help="параметры сервера")
    commands.add_parser("names", help="имена клиентов, по одному в строке")
    fpset = commands.add_parser("fp-links", help="ссылки с разными отпечатками")
    fpset.add_argument("name")
    domain = commands.add_parser("set-domain", help="сменить маскировочный домен")
    domain.add_argument("domain")
    fingerprint = commands.add_parser("set-fingerprint", help="сменить отпечаток ClientHello")
    fingerprint.add_argument("fingerprint", choices=FINGERPRINTS)
    alts = commands.add_parser("set-alts", help="пробные входы: домен:порт …")
    alts.add_argument("pairs", nargs="+")
    commands.add_parser("clear-alts", help="убрать пробные входы")
    altlinks = commands.add_parser("alt-links", help="ссылки на пробные входы")
    altlinks.add_argument("name")
    selftest = commands.add_parser("client-config", help="конфиг клиента для самопроверки")
    selftest.add_argument("name")
    selftest.add_argument("--socks-port", type=int, default=10808)
    selftest.add_argument("--address", default=None)
    cdn = commands.add_parser("cdn-setup", help="вход через CDN")
    cdn.add_argument("--domain", required=True)
    cdn.add_argument("--cert", required=True)
    cdn.add_argument("--key", required=True)
    cdn.add_argument("--path", required=True)
    cdn.add_argument("--port", type=int, default=443)
    cdn.add_argument("--reality-port", type=int, default=8443,
                     help="куда уходит REALITY, если его порт занимает CDN")
    ssup = commands.add_parser("ss-setup", help="вход Shadowsocks")
    ssup.add_argument("--port", type=int, default=443)
    ssup.add_argument("--method", default=SS_METHOD)
    ssup.add_argument("--password", required=True)
    ssup.add_argument("--name", default="ss")
    ssup.add_argument("--reality-port", type=int, default=8443)
    commands.add_parser("ss-link", help="ссылка Shadowsocks")
    commands.add_parser("ss-clear", help="убрать вход Shadowsocks")
    cdnlinks = commands.add_parser("cdn-links", help="ссылки через CDN")
    cdnlinks.add_argument("name", nargs="?")
    commands.add_parser("cdn-clear", help="убрать вход через CDN")
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
                print(f"{client['name']}\n{link(meta, client)}")
                if meta.get("cdn"):
                    print(f"через CDN:\n{cdn_link(meta, client)}")
                print()
        elif args.command == "render":
            write_config(meta, args.config, loglevel=args.loglevel)
            print(f"Конфиг пересобран: {args.config} (журнал: {args.loglevel})")
        elif args.command == "set-alts":
            alternatives = []
            for pair in args.pairs:
                parts = pair.split(":")
                if len(parts) not in (2, 3) or not parts[0] or not parts[1].isdigit():
                    raise VlessError(f"Нужно домен:порт или домен:порт:novision, а не {pair!r}.")
                entry = {"sni": parts[0], "port": int(parts[1])}
                if len(parts) == 3:
                    if parts[2] != "novision":
                        raise VlessError(f"Третье поле может быть только novision, а не {parts[2]!r}.")
                    entry["flow"] = ""
                alternatives.append(entry)
            meta["alts"] = alternatives
            _apply(meta, args.config, args.meta)
            print(f"Пробных входов: {len(alternatives)}")
        elif args.command == "clear-alts":
            meta["alts"] = []
            _apply(meta, args.config, args.meta)
            print("Пробные входы убраны.")
        elif args.command == "alt-links":
            client = find_client(meta, args.name)
            for alt in meta.get("alts", []):
                note = "" if alt.get("flow", FLOW) else ", без Vision"
                print(f"{alt['sni']} (порт {alt['port']}{note})")
                print(link(meta, client, alt))
                print()
        elif args.command == "set-fingerprint":
            meta["fingerprint"] = args.fingerprint
            # Конфиг сервера от отпечатка не зависит — меняются только
            # ссылки, поэтому перезапуск службы не нужен.
            save_meta(meta, args.meta)
            print(args.fingerprint)
        elif args.command == "set-domain":
            meta["sni"] = args.domain
            meta["dest"] = f"{args.domain}:443"
            _apply(meta, args.config, args.meta)
            print(args.domain)
        elif args.command == "fp-links":
            client = find_client(meta, args.name)
            # chrome — как сейчас; safari/firefox — другой стек TLS;
            # randomized прячет отпечаток вовсе. Если режут по нему,
            # какой-то из этих проходит там, где chrome нет.
            for fp in ("chrome", "safari", "firefox", "ios", "randomized"):
                variant = {**meta, "fingerprint": fp}
                print(f"{fp}:")
                print(link(variant, client, label_suffix=f"-{fp}"))
                print()
        elif args.command == "names":
            for client in meta.get("clients", []):
                print(client["name"])
        elif args.command == "client-config":
            config = client_config(
                meta, find_client(meta, args.name), args.socks_port, args.address
            )
            print(json.dumps(config, ensure_ascii=False, indent=2))
        elif args.command == "cdn-setup":
            if not args.path.startswith("/"):
                raise VlessError("Путь должен начинаться с /")
            meta["cdn"] = {
                "domain": args.domain,
                "cert": args.cert,
                "key": args.key,
                "path": args.path,
                "port": args.port,
            }
            # Два входа на одном порту не бывает: REALITY уступает.
            if int(meta["port"]) == args.port:
                meta["port"] = args.reality_port
            _apply(meta, args.config, args.meta)
            for client in meta.get("clients", []):
                print(cdn_link(meta, client))
        elif args.command == "ss-setup":
            if meta.get("cdn") and int(meta["cdn"].get("port", 443)) == args.port:
                raise VlessError(f"Порт {args.port} занят входом через CDN.")
            meta["ss"] = {
                "port": args.port,
                "method": args.method,
                "password": args.password,
                "name": args.name,
            }
            # Два входа на одном порту не бывает: REALITY уступает.
            if int(meta["port"]) == args.port:
                meta["port"] = args.reality_port
            _apply(meta, args.config, args.meta)
            print(ss_link(meta))
        elif args.command == "ss-link":
            if not meta.get("ss"):
                raise VlessError("Вход Shadowsocks не настроен: sudo sh deploy/vless.sh ss")
            print(ss_link(meta))
        elif args.command == "ss-clear":
            meta.pop("ss", None)
            _apply(meta, args.config, args.meta)
            print("Вход Shadowsocks убран.")
        elif args.command == "cdn-links":
            if not meta.get("cdn"):
                raise VlessError("Вход через CDN не настроен: sudo sh deploy/vless.sh cdn домен")
            targets = [find_client(meta, args.name)] if args.name else meta.get("clients", [])
            for client in targets:
                print(f"{client['name']}\n{cdn_link(meta, client)}\n")
        elif args.command == "cdn-clear":
            meta.pop("cdn", None)
            _apply(meta, args.config, args.meta)
            print("Вход через CDN убран.")
        elif args.command == "get":
            # Вложенные поля через точку: get cdn.domain. Иначе shell
            # разбирал бы питоновский repr словаря через sed.
            value = meta
            for part in args.field.split("."):
                value = value.get(part) if isinstance(value, dict) else None
                if value is None:
                    raise VlessError(f"В шпаргалке нет поля {args.field!r}.")
            print(value)
        elif args.command == "show":
            print(f"адрес:  {meta['host']}:{meta['port']}")
            print(f"маска:  {meta['sni']}")
            if meta.get("cdn"):
                print(f"CDN:    {meta['cdn']['domain']}:{meta['cdn'].get('port', 443)}")
            if meta.get("ss"):
                print(f"SS:     порт {meta['ss']['port']}, {meta['ss']['method']}")
            print(f"клиентов: {len(meta.get('clients', []))}")
    except VlessError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
