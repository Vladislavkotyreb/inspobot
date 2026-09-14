#!/usr/bin/env python3
"""Доходят ли до сервера из России.

Смена маскировочного домена бессмысленна, если заблокирован сам адрес,
а отличить одно от другого с сервера невозможно: он видит только свою
сторону. Проверка идёт через check-host.net — у него есть узлы внутри
России, и он умеет просто постучаться в TCP-порт.

Это внешняя служба, и она может измениться или не ответить. Поэтому
любой сбой здесь — «проверить не удалось», а не «заблокировано»:
ложное обвинение хуже отсутствия ответа.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API = "https://check-host.net"
AGENT = "inspobot-vless-reach"
POLL_SECONDS = 3
POLL_TRIES = 8


class ReachError(Exception):
    """Проверку выполнить не удалось — это не приговор адресу."""


def _get(url: str, timeout: float = 25.0) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        raise ReachError(f"{type(error).__name__}: {str(error)[:80]}")


def node_place(info) -> tuple[str, str]:
    """Страна и город узла из описания check-host.net.

    Формат — список вида ["ru", "Russia", "Moscow", ...]. Он менялся,
    поэтому читаем мягко: чего нет, то пустая строка.
    """
    if not isinstance(info, list):
        return ("", "")
    country = str(info[0]).lower() if len(info) > 0 and info[0] else ""
    city = str(info[2]) if len(info) > 2 and info[2] else ""
    return (country, city)


def node_verdict(result) -> tuple[bool, str]:
    """Достучались ли с этого узла.

    Успех — список словарей с полем time. Отказ — с полем error.
    None означает «узел ещё не ответил», это не отказ.
    """
    if result is None:
        return (False, "ещё считает")
    if not isinstance(result, list) or not result:
        return (False, "пусто")
    first = result[0]
    if not isinstance(first, dict):
        return (False, "непонятный ответ")
    if first.get("error"):
        return (False, str(first["error"])[:40])
    if "time" in first:
        return (True, f"{float(first['time']) * 1000:.0f} мс")
    return (False, "без результата")


def summarize(nodes: dict, results: dict, country: str = "ru") -> dict:
    """Сводка по стране: сколько узлов достучалось."""
    rows = []
    for name, info in sorted(nodes.items()):
        place = node_place(info)
        if country and place[0] != country:
            continue
        ok, note = node_verdict(results.get(name))
        rows.append({"node": name, "city": place[1], "ok": ok, "note": note})
    reached = sum(1 for row in rows if row["ok"])
    return {"rows": rows, "reached": reached, "total": len(rows)}


def check(host: str, port: int, nodes: int = 30) -> tuple[dict, dict]:
    started = _get(f"{API}/check-tcp?host={host}%3A{port}&max_nodes={nodes}")
    request_id = started.get("request_id")
    if not request_id:
        raise ReachError("служба не выдала номер проверки")
    known = started.get("nodes") or {}
    results: dict = {}
    for _ in range(POLL_TRIES):
        time.sleep(POLL_SECONDS)
        results = _get(f"{API}/check-result/{request_id}")
        if results and all(value is not None for value in results.values()):
            break
    return (known, results or {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Доходят ли до адреса из России")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--country", default="ru", help="пусто — все страны")
    parser.add_argument("--nodes", type=int, default=30)
    args = parser.parse_args(argv)

    print(f"Проверяю {args.host}:{args.port} с узлов check-host.net...")
    try:
        nodes, results = check(args.host, args.port, args.nodes)
    except ReachError as error:
        print(f"Проверить не удалось: {error}", file=sys.stderr)
        print("Это не значит, что адрес заблокирован — значит, что служба", file=sys.stderr)
        print("проверки не ответила. Повторите позже.", file=sys.stderr)
        return 2

    report = summarize(nodes, results, args.country)
    if not report["total"]:
        print(f"Узлов в стране {args.country!r} не досталось — попробуйте --country ''")
        return 2

    for row in report["rows"]:
        mark = "  ok   " if row["ok"] else "  нет  "
        place = row["city"] or row["node"]
        print(f"{mark}{place:<24}{row['note']}")

    print()
    reached, total = report["reached"], report["total"]
    print(f"Из России достучались: {reached} из {total}")
    if reached == 0:
        print()
        print("Ни один российский узел не подключился к этому порту.")
        print("Менять маскировочный домен бесполезно: блокируется адрес или порт.")
        return 1
    if reached < total:
        print()
        print("Часть узлов не прошла — блокировка выборочная, у разных")
        print("провайдеров по-разному. У одного не работает, у другого работает.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
