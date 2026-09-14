#!/usr/bin/env python3
"""Варианты конфигурации для перебора.

Когда рукопожатие REALITY не проходит, а ключи, время и порт в порядке,
остаётся перебор: поднять рядом такой же сервер, меняя по одной вещи за
раз, и посмотреть, где заработает. Этот модуль собирает пару конфигов
(сервер + клиент) для каждого варианта; перебирает их deploy/vless.sh.

Живую службу не трогает: всё пишется во временный каталог и слушает
запасной порт.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "vless_admin", os.path.join(_HERE, "vless_admin.py")
)
admin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(admin)

# Каждый вариант отличается от рабочего ровно одной вещью — иначе по
# результату не понять, что именно помогло.
VARIANTS: dict[str, dict] = {
    "как-есть": {},
    # uTLS-отпечаток задаёт форму ClientHello. Свежий «chrome» несёт
    # пост-квантовый обмен ключами, куда REALITY свою аутентификацию
    # встроить не может; у других отпечатков этого нет.
    "отпечаток-firefox": {"fingerprint": "firefox"},
    "отпечаток-safari": {"fingerprint": "safari"},
    "отпечаток-ios": {"fingerprint": "ios"},
    # Маскировочный домен обязан отвечать TLS 1.3 с обычным X25519.
    # Отвечает пост-квантовым — рукопожатие не соберётся.
    "домен-google": {"sni": "dl.google.com"},
    "домен-apple": {"sni": "www.apple.com"},
    "домен-samsung": {"sni": "www.samsung.com"},
    # Vision — отдельный слой поверх REALITY, со своими условиями.
    "без-vision": {"flow": ""},
    # Пустой shortId разрешён и снимает вопрос о его длине.
    "пустой-id": {"short_id": ""},
}


def build(meta: dict, client: dict, variant: str, port: int, socks: int):
    if variant not in VARIANTS:
        raise SystemExit(f"Нет варианта {variant!r}. Есть: {', '.join(VARIANTS)}")
    over = VARIANTS[variant]

    local = copy.deepcopy(meta)
    local["clients"] = [client]
    local["port"] = port
    if "sni" in over:
        local["sni"] = over["sni"]
        local["dest"] = f"{over['sni']}:443"

    server = admin.render_config(local, "info")
    outbound = admin.client_config(local, client, socks, "127.0.0.1")

    reality_in = server["inbounds"][0]["streamSettings"]["realitySettings"]
    reality_out = outbound["outbounds"][0]["streamSettings"]["realitySettings"]
    user = outbound["outbounds"][0]["settings"]["vnext"][0]["users"][0]

    if over.get("short_id") == "":
        reality_in["shortIds"] = [""]
        reality_out["shortId"] = ""
    if "fingerprint" in over:
        reality_out["fingerprint"] = over["fingerprint"]
    if over.get("flow") == "":
        for entry in server["inbounds"][0]["settings"]["clients"]:
            entry.pop("flow", None)
        user.pop("flow", None)

    return server, outbound


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Конфиги для перебора вариантов")
    parser.add_argument("--meta", default=admin.META)
    parser.add_argument("--dir")
    parser.add_argument("--variant")
    parser.add_argument("--client")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--socks", type=int, default=10808)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for name in VARIANTS:
            print(name)
        return 0
    if not args.dir or not args.variant:
        parser.error("нужны --dir и --variant (или --list)")

    meta = admin.load_meta(args.meta)
    clients = meta.get("clients", [])
    if not clients:
        raise SystemExit("В шпаргалке нет клиентов.")
    client = admin.find_client(meta, args.client) if args.client else clients[0]

    server, outbound = build(meta, client, args.variant, args.port, args.socks)
    os.makedirs(args.dir, exist_ok=True)
    for name, data in (("server.json", server), ("client.json", outbound)):
        path = os.path.join(args.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
