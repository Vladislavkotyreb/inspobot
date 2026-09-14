#!/usr/bin/env python3
"""Замер маскировочных доменов.

REALITY отдаёт клиенту не свой ответ, а ответ настоящего сайта, за
который прячется. В модуле reality, закреплённом в Xray 26.x, буфер
этого ответа — 8192 байта (tls.go:140), и запись длиннее обрывает
рукопожатие уже ПОСЛЕ успешной аутентификации. Снаружи это выглядит
как полностью здоровый сервер, к которому никто не может подключиться.

Поэтому домен проверяется не на доступность, а на размер ответа.
Скрипт открывает настоящее TLS 1.3 рукопожатие и считает, сколько
байт прислал сервер, — и сравнивает с порогом.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys

# tls.go:140 в github.com/xtls/reality: size = 8192.
LIMIT = 8192
# Запас: наш ClientHello скромнее, чем у настоящего браузера. Браузер
# просит OCSP-подтверждение и подписи сертификата, и ответ вырастает.
# Домен у самой черты сегодня уложится, а завтра нет.
SAFE = 6144

CANDIDATES = (
    "dl.google.com",
    "www.bing.com",
    "www.samsung.com",
    "www.apple.com",
    "addons.mozilla.org",
    "www.asus.com",
    "www.nvidia.com",
    "www.lg.com",
    "swdist.apple.com",
    "yahoo.com",
)


class Measurement:
    def __init__(self, host: str):
        self.host = host
        self.bytes = 0
        self.version = ""
        self.alpn = ""
        self.cipher = ""
        self.error = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.version == "TLSv1.3" and self.bytes <= LIMIT

    @property
    def verdict(self) -> str:
        if self.error:
            return f"не отвечает ({self.error})"
        if self.version != "TLSv1.3":
            return f"не TLS 1.3 ({self.version})"
        if self.bytes > LIMIT:
            return f"ответ {self.bytes} Б — БОЛЬШЕ предела {LIMIT}, рукопожатие оборвётся"
        if self.bytes > SAFE:
            return f"ответ {self.bytes} Б — впритык к пределу {LIMIT}"
        return f"ответ {self.bytes} Б"


def measure(host: str, timeout: float = 12.0) -> Measurement:
    result = Measurement(host)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    # Нас интересует размер ответа, а не доверие к сертификату: проверка
    # цепочки тут только мешает — упадёт на своей ошибке и не покажет
    # того, ради чего замер делается.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2", "http/1.1"])

    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    handshake = context.wrap_bio(incoming, outgoing, server_hostname=host)

    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            raw.settimeout(timeout)
            while True:
                try:
                    handshake.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    pending = outgoing.read()
                    if pending:
                        raw.sendall(pending)
                    chunk = raw.recv(16384)
                    if not chunk:
                        raise ssl.SSLError("соединение закрыто на рукопожатии")
                    result.bytes += len(chunk)
                    incoming.write(chunk)
            result.version = handshake.version() or ""
            result.alpn = handshake.selected_alpn_protocol() or ""
            cipher = handshake.cipher()
            result.cipher = cipher[0] if cipher else ""
    except (OSError, ssl.SSLError, socket.timeout) as error:
        result.error = type(error).__name__ if not str(error) else str(error)[:60]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Замер маскировочных доменов")
    parser.add_argument("domains", nargs="*", default=None)
    parser.add_argument("--best", action="store_true", help="напечатать один лучший домен")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    hosts = args.domains or list(CANDIDATES)
    measured = [measure(host) for host in hosts]
    good = [item for item in measured if item.ok]
    good.sort(key=lambda item: item.bytes)

    if args.best:
        if not good:
            print("подходящих доменов нет", file=sys.stderr)
            return 1
        print(good[0].host)
        return 0

    if not args.quiet:
        for item in measured:
            mark = "  ok   " if item.ok else "  нет  "
            print(f"{mark}{item.host:<22}{item.verdict}")
        print()
        if good:
            print(f"Лучший: {good[0].host} ({good[0].bytes} Б)")
        else:
            print(f"Ни один домен не уложился в {LIMIT} Б.")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
