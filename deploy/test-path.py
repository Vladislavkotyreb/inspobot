#!/usr/bin/env python3
"""Где именно рвётся путь до сервера.

Запускать НА СВОЁМ компьютере, не на сервере:

    python3 deploy/test-path.py --host 202.61.225.12 --sni dl.google.com

Проверяет путь слоями и показывает, какой слой не прошёл:

  1. TCP  — доходит ли вообще соединение;
  2. TLS  — проходит ли рукопожатие с этим именем домена;
  3. Поток — сколько данных удаётся получить, прежде чем всё замрёт.

Третий слой нужен из-за того, как устроены ограничения: соединение
устанавливается, рукопожатие проходит, а потом поток тихо замирает
после нескольких килобайт, без ошибки и без разрыва. Снаружи это
выглядит как «интернет работает, а VPN нет», и по первым двум слоям
неотличимо от исправного пути.

Третий слой пользуется тем, что REALITY отдаёт постороннему настоящий
сайт: обычный запрос по HTTP через это соединение вернёт настоящую
страницу, и по ней видно, сколько данных доходит.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import time

STALL_SECONDS = 6.0


def step(name: str) -> None:
    print(f"\n=== {name} ===")


def tcp(host: str, port: int, timeout: float = 10.0) -> float | None:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return time.monotonic() - started
    except OSError as error:
        print(f"  НЕТ   не соединяется: {type(error).__name__} {error}")
        return None


def handshake(host: str, port: int, sni: str, timeout: float = 15.0):
    """Рукопожатие и, если прошло, открытое соединение вместе с замерами."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["http/1.1"])
    started = time.monotonic()
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        raw.settimeout(timeout)
        wrapped = context.wrap_socket(raw, server_hostname=sni)
    except (OSError, ssl.SSLError) as error:
        reason = f"{type(error).__name__} {error}".strip()
        print(f"  НЕТ   рукопожатие не прошло: {reason[:90]}")
        return (None, time.monotonic() - started)
    return (wrapped, time.monotonic() - started)


def subject_of(wrapped) -> str:
    try:
        binary = wrapped.getpeercert(binary_form=True)
    except Exception:
        return ""
    return f"{len(binary)} Б" if binary else ""


def stream(wrapped, sni: str) -> tuple[int, bool]:
    """Сколько байт удаётся получить и замер ли поток на полпути."""
    request = (
        f"GET / HTTP/1.1\r\nHost: {sni}\r\n"
        "User-Agent: Mozilla/5.0\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    ).encode()
    wrapped.sendall(request)
    total = 0
    stalled = False
    wrapped.settimeout(STALL_SECONDS)
    while True:
        try:
            chunk = wrapped.recv(16384)
        except (socket.timeout, TimeoutError):
            stalled = True
            break
        except (OSError, ssl.SSLError):
            break
        if not chunk:
            break
        total += len(chunk)
        if total > 400_000:
            break
    return (total, stalled)


def probe(title: str, host: str, port: int, sni: str) -> dict:
    step(title)
    print(f"  цель: {host}:{port}, имя домена: {sni}")
    outcome = {"tcp": None, "tls": None, "bytes": 0, "stalled": False}

    seconds = tcp(host, port)
    if seconds is None:
        return outcome
    outcome["tcp"] = seconds
    print(f"  ok    TCP за {seconds * 1000:.0f} мс")

    wrapped, spent = handshake(host, port, sni)
    if wrapped is None:
        return outcome
    outcome["tls"] = spent
    print(f"  ok    TLS за {spent * 1000:.0f} мс, сертификат {subject_of(wrapped)}")

    total, stalled = stream(wrapped, sni)
    outcome["bytes"], outcome["stalled"] = total, stalled
    try:
        wrapped.close()
    except OSError:
        pass
    if stalled:
        print(f"  НЕТ   поток замер после {total} Б — данные перестали идти")
    else:
        print(f"  ok    получено {total} Б, поток закрылся нормально")
    return outcome


def verdict(ours: dict, control: dict, sni: str) -> str:
    if ours["tcp"] is None:
        return ("До сервера не доходит даже TCP. Блокируется адрес или порт.")
    if ours["tls"] is None:
        return (
            "TCP доходит, а рукопожатие TLS не проходит. Это картина\n"
            "фильтрации по содержимому: соединение пропускают, а TLS режут.\n"
            "Ни смена маскировочного домена, ни смена порта тут не помогут."
        )
    if ours["stalled"] and not control["stalled"]:
        return (
            f"Рукопожатие проходит, а поток замирает после {ours['bytes']} Б,\n"
            f"тогда как к настоящему {sni} данные идут до конца.\n"
            "Это ограничение по объёму на соединения к этому серверу:\n"
            "туннель успевает подняться и умирает на первых же данных."
        )
    if ours["stalled"] and control["stalled"]:
        return (
            "Поток замирает и к серверу, и к настоящему сайту — похоже,\n"
            "дело в сети, из которой идёт проверка, а не в сервере."
        )
    return (
        "Путь до сервера проходит целиком: TCP, рукопожатие и поток данных.\n"
        "Значит, режут не путь, и причину надо искать в самом туннеле."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Где рвётся путь до сервера")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--sni", required=True, help="маскировочный домен сервера")
    args = parser.parse_args(argv)

    ours = probe("наш сервер", args.host, args.port, args.sni)
    # Тот же домен, но настоящий: даёт точку отсчёта. Без неё непонятно,
    # особенное ли это поведение или у вас вся сеть так себя ведёт.
    control = probe(f"для сравнения — настоящий {args.sni}", args.sni, 443, args.sni)

    step("вывод")
    print(verdict(ours, control, args.sni))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
