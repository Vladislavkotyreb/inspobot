"""Проверки VLESS-админки.

Собственно туннель отсюда не проверить — нужен сервер и клиент. Что
проверяемо здесь: конфиг, который уедет в Xray, собирается валидным и
не теряет по дороге ни ключа, ни клиента, а ссылка несёт ровно те
параметры, без которых клиентское приложение не подключится.
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "vless_admin", ROOT / "deploy" / "vless_admin.py"
)
vless = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vless)


def meta(**overrides):
    base = {
        "host": "203.0.113.10",
        "port": 443,
        "sni": "www.microsoft.com",
        "dest": "www.microsoft.com:443",
        "private_key": "ПРИВАТНЫЙ",
        "public_key": "ПУБЛИЧНЫЙ",
        "short_id": "0123456789abcdef",
        "clients": [{"name": "vlad-iphone", "id": "11111111-1111-4111-8111-111111111111"}],
    }
    base.update(overrides)
    return base


class RenderConfig(unittest.TestCase):
    def test_валидный_json_с_нужными_полями(self):
        config = vless.render_config(meta())
        json.dumps(config)  # не должно падать
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["port"], 443)
        self.assertEqual(inbound["protocol"], "vless")
        reality = inbound["streamSettings"]["realitySettings"]
        self.assertEqual(reality["privateKey"], "ПРИВАТНЫЙ")
        self.assertEqual(reality["serverNames"], ["www.microsoft.com"])
        self.assertEqual(reality["dest"], "www.microsoft.com:443")
        self.assertEqual(reality["shortIds"], ["0123456789abcdef"])

    def test_порт_становится_числом(self):
        # Из shell он приходит строкой, а Xray на строке падает.
        config = vless.render_config(meta(port="443"))
        self.assertIsInstance(config["inbounds"][0]["port"], int)

    def test_публичный_ключ_в_конфиг_не_попадает(self):
        # Он нужен только ссылке; в конфиге ему делать нечего.
        self.assertNotIn("ПУБЛИЧНЫЙ", json.dumps(vless.render_config(meta())))

    def test_каждый_клиент_с_flow(self):
        data = meta()
        vless.add_client(data, "mac")
        clients = vless.render_config(data)["inbounds"][0]["settings"]["clients"]
        self.assertEqual([c["email"] for c in clients], ["vlad-iphone", "mac"])
        self.assertTrue(all(c["flow"] == vless.FLOW for c in clients))

    def test_локальная_сеть_закрыта(self):
        # Иначе клиент достаёт по туннелю localhost сервера, где бот.
        rules = vless.render_config(meta())["routing"]["rules"]
        blocked = [r for r in rules if r.get("outboundTag") == "block"]
        addresses = [address for rule in blocked for address in rule.get("ip", [])]
        self.assertIn("127.0.0.0/8", addresses)
        self.assertIn("192.168.0.0/16", addresses)
        self.assertIn("::1/128", addresses)

    def test_торренты_закрыты(self):
        rules = vless.render_config(meta())["routing"]["rules"]
        self.assertTrue(
            any(
                rule.get("outboundTag") == "block"
                and "bittorrent" in rule.get("protocol", [])
                for rule in rules
            )
        )

    def test_журнал_посещений_не_ведётся(self):
        self.assertEqual(vless.render_config(meta())["log"]["access"], "none")

    def test_без_обязательного_поля_падает_до_записи(self):
        with self.assertRaises(vless.VlessError):
            vless.render_config(meta(private_key=""))


class Link(unittest.TestCase):
    def test_все_параметры_на_месте(self):
        data = meta()
        url = vless.link(data, data["clients"][0])
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "vless")
        self.assertEqual(parsed.username, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(parsed.hostname, "203.0.113.10")
        self.assertEqual(parsed.port, 443)
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        self.assertEqual(query["security"], "reality")
        self.assertEqual(query["pbk"], "ПУБЛИЧНЫЙ")
        self.assertEqual(query["sni"], "www.microsoft.com")
        self.assertEqual(query["sid"], "0123456789abcdef")
        self.assertEqual(query["flow"], vless.FLOW)
        self.assertEqual(query["type"], "tcp")
        self.assertEqual(query["encryption"], "none")
        self.assertEqual(query["fp"], vless.FINGERPRINT)

    def test_имя_в_хвосте(self):
        data = meta()
        self.assertTrue(vless.link(data, data["clients"][0]).endswith("#vlad-iphone"))

    def test_ipv6_в_скобках(self):
        data = meta(host="2a03:4000::1")
        url = vless.link(data, data["clients"][0])
        self.assertIn("@[2a03:4000::1]:443", url)
        self.assertEqual(urlparse(url).hostname, "2a03:4000::1")


class Clients(unittest.TestCase):
    def test_новый_клиент_получает_uuid(self):
        data = meta()
        client = vless.add_client(data, "mac")
        self.assertEqual(len(client["id"]), 36)
        self.assertNotEqual(client["id"], data["clients"][0]["id"])

    def test_двух_одинаковых_имён_не_бывает(self):
        data = meta()
        with self.assertRaises(vless.VlessError):
            vless.add_client(data, "vlad-iphone")
        self.assertEqual(len(data["clients"]), 1)

    def test_кривое_имя_отклоняется(self):
        # Имя уходит и в конфиг, и в хвост ссылки — пробелы и решётки там лишние.
        for name in ("", "имя", "два слова", "reality#1", "x" * 33, "-начало"):
            with self.subTest(name=name), self.assertRaises(vless.VlessError):
                vless.add_client(meta(), name)

    def test_удаление(self):
        data = meta()
        vless.remove_client(data, "vlad-iphone")
        self.assertEqual(data["clients"], [])
        with self.assertRaises(vless.VlessError):
            vless.remove_client(data, "vlad-iphone")


class Files(unittest.TestCase):
    def test_запись_и_права(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/nested/reality.json"
            vless.save_meta(meta(), path)
            self.assertEqual(vless.load_meta(path)["sni"], "www.microsoft.com")
            # В файле приватный ключ — читать его можно только владельцу.
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_нет_файла_понятная_ошибка(self):
        with self.assertRaises(vless.VlessError) as caught:
            vless.load_meta("/nonexistent/reality.json")
        self.assertIn("install", str(caught.exception))

    def test_битый_конфиг_не_затирает_рабочий(self):
        # _apply собирает конфиг первым: если не собрался — на диске
        # остаётся прежняя пара файлов, а не половина новой.
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            vless._apply(meta(), config, meta_path)
            before = pathlib.Path(config).read_text(encoding="utf-8")
            with self.assertRaises(vless.VlessError):
                vless._apply(meta(short_id=""), config, meta_path)
            self.assertEqual(pathlib.Path(config).read_text(encoding="utf-8"), before)


def run_cli(*args):
    """Прогон команды без печати в отчёт теста."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return vless.main(list(args))


class Cli(unittest.TestCase):
    def test_init_add_list_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            code = run_cli(*common, *[
                "init", "--host", "203.0.113.10", "--port", "443",
                "--sni", "www.microsoft.com", "--dest", "www.microsoft.com:443",
                "--private-key", "PRIV", "--public-key", "PUB",
                "--short-id", "abc123", "--client", "vlad-iphone",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(run_cli(*common, "add", "mac"), 0)
            self.assertEqual(len(vless.load_meta(meta_path)["clients"]), 2)
            written = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertEqual(len(written["inbounds"][0]["settings"]["clients"]), 2)
            self.assertEqual(run_cli(*common, "remove", "mac"), 0)
            self.assertEqual(len(vless.load_meta(meta_path)["clients"]), 1)
            # Конфиг пересобран вслед за шпаргалкой, а не отстал от неё.
            written = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertEqual(len(written["inbounds"][0]["settings"]["clients"]), 1)

    def test_повтор_имени_возвращает_код_ошибки(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, *[
                "init", "--host", "h", "--port", "443", "--sni", "s",
                "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                "--short-id", "a", "--client", "one",
            ])
            self.assertEqual(run_cli(*common, "add", "one"), 1)


if __name__ == "__main__":
    unittest.main()
