#!/usr/bin/env python3
"""AmneziaWG: конфиги сервера и клиентов.

AmneziaWG — WireGuard с забитой сигнатурой: к обычному протоколу
добавлены мусорные пакеты и подменённые заголовки, чтобы фильтры не
опознавали его по первому же пакету. Работает поверх UDP, то есть мимо
всего, что происходит с TLS.

Параметры обфускации обязаны совпадать у сервера и у каждого клиента
до единого числа: это не настройка, а часть протокола. Поэтому они
живут в одном месте и подставляются в оба конфига отсюда.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys

DIR = "/etc/amnezia/amneziawg"
META = f"{DIR}/peers.json"
CONF = f"{DIR}/awg0.conf"
IFACE = "awg0"
SUBNET = "10.8.0"
# 55424, а не 51820: 51820 — штатный порт WireGuard, и его режут по
# одному номеру, не заглядывая внутрь. Значение взято из эталонных
# настроек Amnezia.
PORT = 55424
# 1280 — наименьший MTU, гарантированный для IPv6, и он же спасает от
# фрагментации в мобильных сетях, где путь бывает уже обычного.
MTU = 1280
DNS = "1.1.1.1, 8.8.8.8"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# Поддельный DNS-запрос к icloud.com: первый пакет клиента выглядит как
# обычное обращение к имени, а не как начало туннеля. Значение — из
# эталонных настроек Amnezia, менять его смысла нет.
SPECIAL_JUNK_1 = (
    "<r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001"
    "c00c000100010000105a00044d583737>"
)

# Порядок важен: в конфиг они пишутся именно так.
PARAM_ORDER = (
    "Jc", "Jmin", "Jmax",
    "S1", "S2", "S3", "S4",
    "H1", "H2", "H3", "H4",
    "HeaderProtectionKey", "ContentPaddingAddition",
    "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
    "KeepaliveTimeout", "MaxHandshakeAttempts",
    "RandomTrailers", "DisableCookies",
)


class AwgError(Exception):
    """Ошибка, которую надо показать человеку."""


def make_params(rng: random.Random | None = None, header_key: str | None = None) -> dict:
    """Параметры обфускации по эталону Amnezia (протокол 3.1).

    Заголовки H1..H4 здесь штатные — 1, 2, 3, 4, — а не случайные, как
    было в первой версии AmneziaWG. Прятать их рандомизацией больше не
    нужно: HeaderProtectionKey шифрует их целиком, и это сильнее.
    Случайные же заголовки, наоборот, сами по себе примета.

    Значения взяты из amnezia-client (protocolConstants.h,
    awgInstaller.cpp): это то, что раздаёт их собственное приложение, и
    оно работает у живых людей.
    """
    rng = rng or random.SystemRandom()
    return {
        "Jc": rng.randint(4, 6),
        "Jmin": 10,
        "Jmax": 50,
        "S1": 12,
        "S2": 12,
        "S3": 12,
        "S4": 12,
        "H1": 1,
        "H2": 2,
        "H3": 3,
        "H4": 4,
        "HeaderProtectionKey": header_key or genkey(),
        "ContentPaddingAddition": "10-100",
        "RekeyAfterTime": "100-120",
        "RekeyTimeout": "3-7",
        "RejectAfterTime": "150-180",
        "KeepaliveTimeout": "5-15",
        "MaxHandshakeAttempts": "15-20",
        "RandomTrailers": "on",
        "DisableCookies": "on",
    }


def check_params(params: dict) -> None:
    """Ограничения протокола. Нарушение любого даёт туннель, который
    поднимается и молчит без единой ошибки в журнале."""
    for key in PARAM_ORDER:
        if key not in params:
            raise AwgError(f"В параметрах обфускации нет {key}.")
    if not 1 <= int(params["Jc"]) <= 128:
        raise AwgError("Jc должен быть от 1 до 128.")
    if int(params["Jmin"]) >= int(params["Jmax"]):
        raise AwgError("Jmin должен быть меньше Jmax.")
    if int(params["Jmax"]) > 1280:
        raise AwgError("Jmax не больше 1280.")
    for key in ("S1", "S2", "S3", "S4"):
        if not 0 <= int(params[key]) <= 150:
            raise AwgError(f"{key} должен быть от 0 до 150.")
    # Замусоренный пакет не должен совпасть по длине с настоящим.
    if int(params["S1"]) + 56 == int(params["S2"]):
        raise AwgError("S1 + 56 не должно равняться S2.")
    headers = [int(params[f"H{n}"]) for n in (1, 2, 3, 4)]
    if len(set(headers)) != 4:
        raise AwgError("H1..H4 должны быть разными.")
    if not params.get("HeaderProtectionKey"):
        raise AwgError("Без HeaderProtectionKey заголовки не защищены.")
    for key in ("RandomTrailers", "DisableCookies"):
        if params[key] not in ("on", "off"):
            raise AwgError(f"{key} — это on или off.")


def _params_block(params: dict) -> str:
    return "\n".join(f"{key} = {params[key]}" for key in PARAM_ORDER)


def server_config(meta: dict) -> str:
    check_params(meta["params"])
    iface = meta.get("wan", "eth0")
    net = f"{SUBNET}.0/24"
    lines = [
        "[Interface]",
        f"Address = {SUBNET}.1/24",
        f"ListenPort = {meta['port']}",
        f"PrivateKey = {meta['private_key']}",
        f"MTU = {MTU}",
        _params_block(meta["params"]),
        # Без пересылки и подмены адреса туннель поднимется, но наружу
        # из него ничего не пойдёт — самая частая причина «подключился,
        # а интернета нет».
        f"PostUp = sysctl -w net.ipv4.ip_forward=1",
        f"PostUp = iptables -t nat -A POSTROUTING -s {net} -o {iface} -j MASQUERADE",
        f"PostUp = iptables -A FORWARD -i %i -j ACCEPT",
        f"PostUp = iptables -A FORWARD -o %i -j ACCEPT",
        f"PostDown = iptables -t nat -D POSTROUTING -s {net} -o {iface} -j MASQUERADE",
        f"PostDown = iptables -D FORWARD -i %i -j ACCEPT",
        f"PostDown = iptables -D FORWARD -o %i -j ACCEPT",
    ]
    for client in meta.get("clients", []):
        lines += [
            "",
            "[Peer]",
            f"# {client['name']}",
            f"PublicKey = {client['public_key']}",
            f"PresharedKey = {client['preshared_key']}",
            f"AllowedIPs = {client['address']}/32",
        ]
    return "\n".join(lines) + "\n"


def client_config(meta: dict, client: dict) -> str:
    check_params(meta["params"])
    return "\n".join([
        "[Interface]",
        f"Address = {client['address']}/32",
        f"PrivateKey = {client['private_key']}",
        f"DNS = {DNS}",
        f"MTU = {MTU}",
        _params_block(meta["params"]),
        f"I1 = {SPECIAL_JUNK_1}",
        "",
        "[Peer]",
        f"PublicKey = {meta['public_key']}",
        f"PresharedKey = {client['preshared_key']}",
        f"Endpoint = {meta['host']}:{meta['port']}",
        "AllowedIPs = 0.0.0.0/0, ::/0",
        # Мобильные сети рвут неактивные соединения через минуту-две;
        # без этого туннель «есть», но первый пакет уходит в никуда.
        "PersistentKeepalive = 25",
    ]) + "\n"


# --- ключи и клиенты -------------------------------------------------


def genkey() -> str:
    return subprocess.run(["awg", "genkey"], capture_output=True, text=True,
                          check=True).stdout.strip()


def pubkey(private: str) -> str:
    return subprocess.run(["awg", "pubkey"], input=private, capture_output=True,
                          text=True, check=True).stdout.strip()


def genpsk() -> str:
    return subprocess.run(["awg", "genpsk"], capture_output=True, text=True,
                          check=True).stdout.strip()


def next_address(meta: dict) -> str:
    used = {client["address"] for client in meta.get("clients", [])}
    for octet in range(2, 255):
        candidate = f"{SUBNET}.{octet}"
        if candidate not in used:
            return candidate
    raise AwgError("Адреса в подсети кончились.")


def check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise AwgError(
            f"Имя {name!r} не годится. Латиница, цифры, точка, дефис, "
            "подчёркивание; до 32 знаков."
        )
    return name


def add_client(meta: dict, name: str) -> dict:
    check_name(name)
    if any(client["name"] == name for client in meta.get("clients", [])):
        raise AwgError(f"Клиент {name!r} уже есть.")
    private = genkey()
    client = {
        "name": name,
        "private_key": private,
        "public_key": pubkey(private),
        "preshared_key": genpsk(),
        "address": next_address(meta),
    }
    meta.setdefault("clients", []).append(client)
    return client


def remove_client(meta: dict, name: str) -> dict:
    clients = meta.get("clients", [])
    for index, client in enumerate(clients):
        if client["name"] == name:
            return clients.pop(index)
    raise AwgError(f"Клиента {name!r} нет.")


def find_client(meta: dict, name: str) -> dict:
    for client in meta.get("clients", []):
        if client["name"] == name:
            return client
    raise AwgError(f"Клиента {name!r} нет.")


# --- файлы -----------------------------------------------------------


def load(path: str = META) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise AwgError(f"Нет {path} — AmneziaWG ещё не установлен.")
    except json.JSONDecodeError as error:
        raise AwgError(f"{path} не читается как JSON: {error}")


def write(path: str, text: str, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    temporary = path + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def apply(meta: dict, conf: str = CONF, meta_path: str = META) -> None:
    """Сначала конфиг, потом шпаргалка: не собрался — на диске прежняя пара."""
    write(conf, server_config(meta))
    write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AmneziaWG: сервер и клиенты")
    parser.add_argument("--meta", default=META)
    parser.add_argument("--conf", default=CONF)
    commands = parser.add_subparsers(dest="command", required=True)

    initial = commands.add_parser("init")
    initial.add_argument("--host", required=True)
    initial.add_argument("--wan", required=True)
    initial.add_argument("--port", type=int, default=PORT)
    initial.add_argument("--client", default="phone")

    for name in ("add", "remove", "config"):
        sub = commands.add_parser(name)
        sub.add_argument("name")
    commands.add_parser("names")
    commands.add_parser("render")
    commands.add_parser("show")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            private = genkey()
            meta = {
                "host": args.host,
                "wan": args.wan,
                "port": args.port,
                "private_key": private,
                "public_key": pubkey(private),
                "params": make_params(),
                "clients": [],
            }
            client = add_client(meta, args.client)
            apply(meta, args.conf, args.meta)
            print(client["name"])
            return 0

        meta = load(args.meta)
        if args.command == "add":
            client = add_client(meta, args.name)
            apply(meta, args.conf, args.meta)
            print(client_config(meta, client))
        elif args.command == "remove":
            remove_client(meta, args.name)
            apply(meta, args.conf, args.meta)
            print(f"Удалён: {args.name}")
        elif args.command == "config":
            print(client_config(meta, find_client(meta, args.name)))
        elif args.command == "names":
            for client in meta.get("clients", []):
                print(client["name"])
        elif args.command == "render":
            write(args.conf, server_config(meta))
            print(args.conf)
        elif args.command == "show":
            print(f"адрес:    {meta['host']}:{meta['port']}")
            print(f"клиентов: {len(meta.get('clients', []))}")
    except AwgError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
