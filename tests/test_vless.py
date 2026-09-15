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
import grp
import os
import pathlib
import pwd
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

PROBE_SPEC = importlib.util.spec_from_file_location(
    "vless_probe", ROOT / "deploy" / "vless_probe.py"
)
probe = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(probe)

DOMAINS_SPEC = importlib.util.spec_from_file_location(
    "vless_domains", ROOT / "deploy" / "vless_domains.py"
)
domains = importlib.util.module_from_spec(DOMAINS_SPEC)
DOMAINS_SPEC.loader.exec_module(domains)

LINK_SPEC = importlib.util.spec_from_file_location(
    "vless_link", ROOT / "deploy" / "vless_link.py"
)
linkmod = importlib.util.module_from_spec(LINK_SPEC)
LINK_SPEC.loader.exec_module(linkmod)

PATH_SPEC = importlib.util.spec_from_file_location(
    "vless_path", ROOT / "deploy" / "test-path.py"
)
pathmod = importlib.util.module_from_spec(PATH_SPEC)
PATH_SPEC.loader.exec_module(pathmod)

REACH_SPEC = importlib.util.spec_from_file_location(
    "vless_reach", ROOT / "deploy" / "vless_reach.py"
)
reach = importlib.util.module_from_spec(REACH_SPEC)
REACH_SPEC.loader.exec_module(reach)


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


    def test_уровень_журнала_меняется(self):
        # Для разбора полётов нужен debug, в обычной жизни — warning.
        self.assertEqual(vless.render_config(meta())["log"]["loglevel"], "warning")
        self.assertEqual(vless.render_config(meta(), "debug")["log"]["loglevel"], "debug")
        # Журнал посещений выключен при любом уровне.
        self.assertEqual(vless.render_config(meta(), "debug")["log"]["access"], "none")

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


class Permissions(unittest.TestCase):
    """Права на конфиг.

    Xray работает не от root, а от пользователя из юнита (штатно —
    `nobody`). Конфиг в 0600 от root он открыть не может и падает с
    `permission denied`, про права при этом не говоря ни слова. Так
    установка и легла в первый раз.
    """

    def unit(self, directory, body):
        path = f"{directory}/xray.service"
        pathlib.Path(path).write_text(body, encoding="utf-8")
        return path

    def test_группа_из_строки_user(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = self.unit(directory, "[Service]\nUser=root\nExecStart=/x\n")
            self.assertEqual(vless.service_gid(unit), pwd.getpwnam("root").pw_gid)

    def test_явная_группа_важнее_пользователя(self):
        own = grp.getgrgid(os.getgid()).gr_name
        with tempfile.TemporaryDirectory() as directory:
            unit = self.unit(directory, f"[Service]\nUser=root\nGroup={own}\n")
            self.assertEqual(vless.service_gid(unit), os.getgid())

    def test_по_умолчанию_nobody(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = self.unit(directory, "[Service]\nExecStart=/x\n")
            try:
                expected = pwd.getpwnam("nobody").pw_gid
            except KeyError:
                self.skipTest("в системе нет пользователя nobody")
            self.assertEqual(vless.service_gid(unit), expected)

    def test_нет_юнита_нет_группы(self):
        self.assertIsNone(vless.service_gid("/nonexistent/xray.service"))

    def test_неизвестный_пользователь(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = self.unit(directory, "[Service]\nUser=такого-нет\n")
            self.assertIsNone(vless.service_gid(unit))

    @unittest.skipUnless(os.geteuid() == 0, "смена владельца требует root")
    def test_конфиг_доступен_группе_демона(self):
        own = grp.getgrgid(os.getgid()).gr_name
        with tempfile.TemporaryDirectory() as directory:
            unit = self.unit(directory, f"[Service]\nUser=root\nGroup={own}\n")
            config = f"{directory}/xray/config.json"
            vless.write_config(meta(), config, unit)
            state = os.stat(config)
            self.assertEqual(stat.S_IMODE(state.st_mode), 0o640)
            self.assertEqual(state.st_gid, os.getgid())
            # В каталог демону надо хотя бы войти.
            mode = stat.S_IMODE(os.stat(f"{directory}/xray").st_mode)
            self.assertTrue(mode & stat.S_IXOTH, oct(mode))

    def test_шпаргалка_остаётся_закрытой(self):
        # В ней приватный ключ, и Xray в неё не заглядывает.
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/reality.json"
            vless.save_meta(meta(), path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_без_прав_на_смену_владельца_файл_остаётся_закрытым(self):
        # Лучше служба, которая не поднялась и сказала об этом в журнал,
        # чем ключ, тихо разложенный в 0644 для всех.
        with tempfile.TemporaryDirectory() as directory:
            unit = self.unit(directory, "[Service]\nUser=root\n")
            config = f"{directory}/config.json"
            original = vless.os.chown

            def refuse(*args, **kwargs):
                raise PermissionError(1, "нельзя")

            vless.os.chown = refuse
            try:
                vless.write_config(meta(), config, unit)
            finally:
                vless.os.chown = original
            self.assertEqual(stat.S_IMODE(os.stat(config).st_mode), 0o600)


class ClientConfig(unittest.TestCase):
    """Конфиг для самопроверки: тот же туннель, что пойдёт на телефон."""

    def test_совпадает_со_ссылкой(self):
        # Разойдутся — самопроверка станет врать: покажет рабочим
        # туннель, которого у клиента нет.
        data = meta()
        client = data["clients"][0]
        config = vless.client_config(data, client)
        json.dumps(config)
        outbound = config["outbounds"][0]
        user = outbound["settings"]["vnext"][0]["users"][0]
        reality = outbound["streamSettings"]["realitySettings"]
        query = {k: v[0] for k, v in parse_qs(urlparse(vless.link(data, client)).query).items()}
        self.assertEqual(user["id"], client["id"])
        self.assertEqual(user["flow"], query["flow"])
        self.assertEqual(reality["publicKey"], query["pbk"])
        self.assertEqual(reality["serverName"], query["sni"])
        self.assertEqual(reality["shortId"], query["sid"])
        self.assertEqual(reality["fingerprint"], query["fp"])
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], data["host"])
        self.assertEqual(outbound["settings"]["vnext"][0]["port"], data["port"])

    def test_socks_только_на_локальном_адресе(self):
        # Иначе временная проверка на минуту открывает наружу
        # незапароленный прокси.
        data = meta()
        inbound = vless.client_config(data, data["clients"][0])["inbounds"][0]
        self.assertEqual(inbound["listen"], "127.0.0.1")

    def test_порт_можно_сменить(self):
        config = vless.client_config(meta(), meta()["clients"][0], 10809)
        self.assertEqual(config["inbounds"][0]["port"], 10809)

    def test_порт_становится_числом(self):
        config = vless.client_config(meta(port="443"), meta()["clients"][0], "10808")
        self.assertEqual(config["outbounds"][0]["settings"]["vnext"][0]["port"], 443)
        self.assertEqual(config["inbounds"][0]["port"], 10808)


    def test_адрес_подменяется_а_sni_нет(self):
        # Reality смотрит на имя, а не на адрес: через 127.0.0.1
        # проверяется рукопожатие, минуя сеть хостера.
        data = meta()
        config = vless.client_config(data, data["clients"][0], address="127.0.0.1")
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "127.0.0.1")
        self.assertEqual(
            outbound["streamSettings"]["realitySettings"]["serverName"], data["sni"]
        )

    def test_без_подмены_берётся_адрес_из_шпаргалки(self):
        data = meta()
        config = vless.client_config(data, data["clients"][0], address=None)
        self.assertEqual(
            config["outbounds"][0]["settings"]["vnext"][0]["address"], data["host"]
        )


class Probe(unittest.TestCase):
    """Варианты для перебора.

    Каждый вариант поднимает пару «сервер + клиент». Если вариант
    поменяет что-то на одной стороне и забудет на другой, перебор
    покажет «не работает» там, где сломал он сам, — и уведёт разбор
    в сторону.
    """

    def pair(self, variant):
        data = meta()
        return probe.build(data, data["clients"][0], variant, 8443, 10808)

    def test_каждый_вариант_собирается(self):
        for variant in probe.VARIANTS:
            with self.subTest(variant=variant):
                server, client = self.pair(variant)
                json.dumps(server)
                json.dumps(client)

    def test_стороны_согласованы(self):
        for variant in probe.VARIANTS:
            with self.subTest(variant=variant):
                server, client = self.pair(variant)
                reality_in = server["inbounds"][0]["streamSettings"]["realitySettings"]
                reality_out = client["outbounds"][0]["streamSettings"]["realitySettings"]
                self.assertEqual(reality_in["serverNames"], [reality_out["serverName"]])
                self.assertIn(reality_out["shortId"], reality_in["shortIds"])
                self.assertTrue(reality_in["dest"].startswith(reality_out["serverName"]))
                user = client["outbounds"][0]["settings"]["vnext"][0]["users"][0]
                entry = server["inbounds"][0]["settings"]["clients"][0]
                self.assertEqual(entry.get("flow"), user.get("flow"))
                self.assertEqual(entry["id"], user["id"])

    def test_порт_и_адрес_пробные(self):
        server, client = self.pair("как-есть")
        self.assertEqual(server["inbounds"][0]["port"], 8443)
        vnext = client["outbounds"][0]["settings"]["vnext"][0]
        self.assertEqual(vnext["port"], 8443)
        # Пробуем через петлю: сеть хостера тут ни при чём.
        self.assertEqual(vnext["address"], "127.0.0.1")

    def test_вариант_меняет_ровно_одно(self):
        base_server, base_client = self.pair("как-есть")
        for variant in probe.VARIANTS:
            if variant == "как-есть":
                continue
            with self.subTest(variant=variant):
                server, client = self.pair(variant)
                self.assertNotEqual(
                    (json.dumps(server, sort_keys=True), json.dumps(client, sort_keys=True)),
                    (json.dumps(base_server, sort_keys=True), json.dumps(base_client, sort_keys=True)),
                    "вариант ничем не отличается от базового",
                )

    def test_живую_шпаргалку_не_портит(self):
        # build работает на копии: иначе перебор испортил бы рабочий конфиг.
        data = meta()
        before = json.dumps(data, sort_keys=True)
        probe.build(data, data["clients"][0], "домен-google", 8443, 10808)
        self.assertEqual(json.dumps(data, sort_keys=True), before)

    def test_неизвестный_вариант_отклоняется(self):
        data = meta()
        with self.assertRaises(SystemExit):
            probe.build(data, data["clients"][0], "такого-нет", 8443, 10808)


class Alts(unittest.TestCase):
    """Пробные входы: несколько доменов на запасных портах.

    Нужны, чтобы человек на той стороне перебрал домены сам, импортировав
    несколько ссылок, а не ждал круга переписки на каждый.
    """

    def with_alts(self):
        return meta(alts=[
            {"sni": "www.bing.com", "port": 8443},
            {"sni": "www.samsung.com", "port": 8444},
        ])

    def test_на_каждый_вход_свой_inbound(self):
        config = vless.render_config(self.with_alts())
        self.assertEqual(len(config["inbounds"]), 3)
        ports = [inbound["port"] for inbound in config["inbounds"]]
        self.assertEqual(ports, [443, 8443, 8444])

    def test_ключи_и_клиенты_общие(self):
        # Иначе пробный вход проверял бы не то, что основной.
        config = vless.render_config(self.with_alts())
        first = config["inbounds"][0]
        for inbound in config["inbounds"][1:]:
            self.assertEqual(
                inbound["streamSettings"]["realitySettings"]["privateKey"],
                first["streamSettings"]["realitySettings"]["privateKey"],
            )
            self.assertEqual(
                inbound["streamSettings"]["realitySettings"]["shortIds"],
                first["streamSettings"]["realitySettings"]["shortIds"],
            )
            self.assertEqual(inbound["settings"]["clients"], first["settings"]["clients"])

    def test_домен_и_dest_совпадают_на_каждом_входе(self):
        # Клиент проверяет сертификат по SNI: разъедутся — рукопожатие
        # не соберётся именно на пробном входе, и разбор уйдёт не туда.
        for inbound in vless.render_config(self.with_alts())["inbounds"]:
            reality = inbound["streamSettings"]["realitySettings"]
            self.assertEqual(reality["dest"], reality["serverNames"][0] + ":443")

    def test_теги_различаются(self):
        tags = [inbound["tag"] for inbound in vless.render_config(self.with_alts())["inbounds"]]
        self.assertEqual(len(set(tags)), len(tags))

    def test_ссылка_на_пробный_вход(self):
        data = self.with_alts()
        client = data["clients"][0]
        url = vless.link(data, client, data["alts"][0])
        parsed = urlparse(url)
        self.assertEqual(parsed.port, 8443)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(query["sni"], "www.bing.com")
        # Имя в хвосте отличает ссылки друг от друга в списке клиента.
        self.assertTrue(url.endswith("#vlad-iphone-www.bing.com"), url)

    def test_основная_ссылка_не_изменилась(self):
        data = self.with_alts()
        client = data["clients"][0]
        self.assertEqual(vless.link(data, client), vless.link(meta(), client))

    def test_вход_без_vision(self):
        # Vision — отдельный слой поверх REALITY со своими условиями;
        # пробный вход без него должен быть без него с обеих сторон.
        data = meta(alts=[{"sni": "www.bing.com", "port": 8443, "flow": ""}])
        config = vless.render_config(data)
        main_client = config["inbounds"][0]["settings"]["clients"][0]
        alt_client = config["inbounds"][1]["settings"]["clients"][0]
        self.assertEqual(main_client["flow"], vless.FLOW)
        self.assertNotIn("flow", alt_client)
        url = vless.link(data, data["clients"][0], data["alts"][0])
        self.assertNotIn("flow=", url)
        self.assertTrue(url.endswith("-novision"), url)
        parsed = linkmod.parse(url)
        self.assertEqual(parsed["flow"], "")
        user = linkmod.client_config(url)["outbounds"][0]["settings"]["vnext"][0]["users"][0]
        self.assertNotIn("flow", user)

    def test_основной_вход_не_теряет_vision_из_за_пробного(self):
        data = meta(alts=[{"sni": "www.bing.com", "port": 8443, "flow": ""}])
        self.assertIn("flow=xtls-rprx-vision", vless.link(data, data["clients"][0]))

    def test_без_пробных_входов_конфиг_прежний(self):
        self.assertEqual(len(vless.render_config(meta())["inbounds"]), 1)


class Domains(unittest.TestCase):
    """Замер доменов. Сеть не трогаем — проверяем разбор результата."""

    def measured(self, **fields):
        item = domains.Measurement(fields.get("host", "example.com"))
        item.bytes = fields.get("bytes", 3000)
        item.version = fields.get("version", "TLSv1.3")
        item.error = fields.get("error", "")
        return item

    def test_предел_совпадает_с_кодом_reality(self):
        # tls.go:140 в github.com/xtls/reality: size = 8192.
        self.assertEqual(domains.LIMIT, 8192)
        self.assertLess(domains.SAFE, domains.LIMIT)

    def test_большой_ответ_отвергается(self):
        # www.microsoft.com отдаёт ~8273 Б и рвёт рукопожатие.
        item = self.measured(bytes=8273)
        self.assertFalse(item.ok)
        self.assertIn("8192", item.verdict)

    def test_ровно_на_пределе_проходит(self):
        self.assertTrue(self.measured(bytes=domains.LIMIT).ok)
        self.assertFalse(self.measured(bytes=domains.LIMIT + 1).ok)

    def test_впритык_отмечается_но_не_отвергается(self):
        item = self.measured(bytes=domains.SAFE + 1)
        self.assertTrue(item.ok)
        self.assertIn("впритык", item.verdict)

    def test_не_tls13_отвергается(self):
        self.assertFalse(self.measured(version="TLSv1.2").ok)

    def test_ошибка_отвергается(self):
        self.assertFalse(self.measured(error="timeout").ok)


class Reach(unittest.TestCase):
    """Разбор ответа check-host.net. Сеть не трогаем.

    Цена ошибки здесь высокая: сказать «адрес заблокирован» там, где
    просто не ответила служба проверки, значит отправить человека
    менять сервер вместо настройки.
    """

    NODES = {
        "ru1.node.check-host.net": ["ru", "Russia", "Moscow", "Ru", 55.7, 37.6],
        "ru2.node.check-host.net": ["ru", "Russia", "Saint Petersburg"],
        "de1.node.check-host.net": ["de", "Germany", "Frankfurt"],
    }

    def test_успех_и_отказ_различаются(self):
        self.assertEqual(reach.node_verdict([{"address": "1.2.3.4", "time": 0.0421}])[0], True)
        self.assertEqual(reach.node_verdict([{"error": "Timeout exceeded"}])[0], False)

    def test_время_показывается_в_миллисекундах(self):
        ok, note = reach.node_verdict([{"address": "1.2.3.4", "time": 0.0421}])
        self.assertTrue(ok)
        self.assertEqual(note, "42 мс")

    def test_незаконченный_узел_не_считается_отказом_по_тексту(self):
        ok, note = reach.node_verdict(None)
        self.assertFalse(ok)
        self.assertIn("считает", note)

    def test_мусор_не_ломает_разбор(self):
        for junk in ([], {}, "строка", [None], [[]], [{"что-то": 1}]):
            with self.subTest(junk=junk):
                ok, note = reach.node_verdict(junk)
                self.assertFalse(ok)
                self.assertTrue(note)

    def test_страна_и_город(self):
        self.assertEqual(reach.node_place(["ru", "Russia", "Moscow"]), ("ru", "Moscow"))
        self.assertEqual(reach.node_place(["RU", "Russia"]), ("ru", ""))
        # Формат у службы менялся — короткий и кривой не должны падать.
        self.assertEqual(reach.node_place([]), ("", ""))
        self.assertEqual(reach.node_place(None), ("", ""))

    def test_сводка_только_по_россии(self):
        results = {
            "ru1.node.check-host.net": [{"address": "202.61.225.12", "time": 0.05}],
            "ru2.node.check-host.net": [{"error": "Connection timed out"}],
            "de1.node.check-host.net": [{"address": "202.61.225.12", "time": 0.01}],
        }
        report = reach.summarize(self.NODES, results, "ru")
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["reached"], 1)
        self.assertEqual([row["city"] for row in report["rows"]], ["Moscow", "Saint Petersburg"])

    def test_все_страны_если_пусто(self):
        report = reach.summarize(self.NODES, {}, "")
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["reached"], 0)

    def test_пропавшие_результаты_не_роняют_сводку(self):
        # Служба может вернуть меньше узлов, чем обещала.
        report = reach.summarize(self.NODES, {}, "ru")
        self.assertEqual(report["reached"], 0)
        self.assertEqual(report["total"], 2)


class LinkRoundTrip(unittest.TestCase):
    """Ссылка → конфиг клиента.

    Главный инвариант: конфиг, собранный из ссылки, должен совпадать с
    тем, что сервер собирает из шпаргалки. Разойдутся — проверка
    настоящим клиентом начнёт проверять не то, что уехало человеку.
    """

    def test_разбор_совпадает_со_шпаргалкой(self):
        data = meta()
        client = data["clients"][0]
        parsed = linkmod.parse(vless.link(data, client))
        self.assertEqual(parsed["id"], client["id"])
        self.assertEqual(parsed["host"], data["host"])
        self.assertEqual(parsed["port"], data["port"])
        self.assertEqual(parsed["sni"], data["sni"])
        self.assertEqual(parsed["pbk"], data["public_key"])
        self.assertEqual(parsed["sid"], data["short_id"])
        self.assertEqual(parsed["flow"], vless.FLOW)
        self.assertEqual(parsed["fp"], vless.FINGERPRINT)
        self.assertEqual(parsed["name"], client["name"])

    def test_конфиг_из_ссылки_совпадает_с_конфигом_из_шпаргалки(self):
        data = meta()
        client = data["clients"][0]
        from_link = linkmod.client_config(vless.link(data, client), 10808)
        from_meta = vless.client_config(data, client, 10808)
        left = from_link["outbounds"][0]
        right = from_meta["outbounds"][0]
        self.assertEqual(
            left["settings"]["vnext"][0]["users"][0],
            right["settings"]["vnext"][0]["users"][0],
        )
        for field in ("serverName", "fingerprint", "publicKey", "shortId", "spiderX"):
            self.assertEqual(
                left["streamSettings"]["realitySettings"][field],
                right["streamSettings"]["realitySettings"][field],
                field,
            )

    def test_ссылка_пробного_входа_разбирается(self):
        data = meta(alts=[{"sni": "www.bing.com", "port": 8443}])
        parsed = linkmod.parse(vless.link(data, data["clients"][0], data["alts"][0]))
        self.assertEqual(parsed["port"], 8443)
        self.assertEqual(parsed["sni"], "www.bing.com")

    def test_socks_только_на_петле(self):
        data = meta()
        config = linkmod.client_config(vless.link(data, data["clients"][0]))
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")

    def test_обрезанная_ссылка_отвергается_понятно(self):
        # Ссылку постоянно теряют по дороге: копируют не целиком,
        # обрезают в мессенджере, криво сканируют QR.
        data = meta()
        full = vless.link(data, data["clients"][0])
        for broken, hint in (
            (full.split("&pbk=")[0], "pbk"),
            # TLS теперь законный вариант (маршрут через CDN), поэтому
            # «не REALITY» проверяем на ссылке вовсе без защиты.
            (full.replace("security=reality", "security=none"), "REALITY"),
            ("https://example.com", "vless://"),
            ("vless://@1.2.3.4:443?security=reality&pbk=x", "идентификатор"),
        ):
            with self.subTest(hint=hint):
                with self.assertRaises(linkmod.LinkError) as caught:
                    linkmod.parse(broken)
                self.assertIn(hint, str(caught.exception))

    def test_флоу_без_vision_не_ломает(self):
        data = meta()
        without = vless.link(data, data["clients"][0]).replace("&flow=xtls-rprx-vision", "")
        config = linkmod.client_config(without)
        user = config["outbounds"][0]["settings"]["vnext"][0]["users"][0]
        self.assertNotIn("flow", user)


class PathVerdict(unittest.TestCase):
    """Вывод послойной проверки.

    Здесь легко ошибиться в пользу паники: сказать «блокируют», когда
    просто плохая сеть, — значит отправить человека покупать новый
    сервер вместо того, чтобы починить настройку. Поэтому каждый слой
    разбирается отдельно, и для потока нужна точка отсчёта.
    """

    def result(self, tcp=0.05, tls=0.1, size=200000, stalled=False):
        return {"tcp": tcp, "tls": tls, "bytes": size, "stalled": stalled}

    def test_нет_tcp(self):
        answer = pathmod.verdict(self.result(tcp=None, tls=None), self.result(), "d.com")
        self.assertIn("TCP", answer)
        self.assertIn("адрес или порт", answer)

    def test_есть_tcp_нет_tls(self):
        answer = pathmod.verdict(self.result(tls=None), self.result(), "d.com")
        self.assertIn("рукопожатие", answer)
        # Важно сказать, что перебор доменов и портов не поможет:
        # иначе разбор уйдёт на новый круг.
        self.assertIn("не помогут", answer)

    def test_поток_замирает_только_у_нас(self):
        answer = pathmod.verdict(
            self.result(size=17000, stalled=True), self.result(), "d.com"
        )
        self.assertIn("17000", answer)
        self.assertIn("объёму", answer)

    def test_поток_замирает_везде_значит_сеть(self):
        answer = pathmod.verdict(
            self.result(stalled=True), self.result(stalled=True), "d.com"
        )
        self.assertIn("сети", answer)
        # Обвинять сервер в этом случае нельзя.
        self.assertNotIn("объёму", answer)

    def test_всё_прошло(self):
        answer = pathmod.verdict(self.result(), self.result(), "d.com")
        self.assertIn("целиком", answer)
        self.assertIn("туннеле", answer)


class Fingerprint(unittest.TestCase):
    """Отпечаток ClientHello — свойство ссылки, а не сервера."""

    def test_по_умолчанию_chrome(self):
        data = meta()
        self.assertIn("fp=chrome", vless.link(data, data["clients"][0]))

    def test_смена_меняет_ссылку_и_клиентский_конфиг(self):
        data = meta(fingerprint="safari")
        client = data["clients"][0]
        self.assertIn("fp=safari", vless.link(data, client))
        reality = vless.client_config(data, client)["outbounds"][0]["streamSettings"]["realitySettings"]
        self.assertEqual(reality["fingerprint"], "safari")

    def test_конфиг_сервера_от_отпечатка_не_зависит(self):
        # Поэтому после смены не нужен перезапуск службы.
        before = json.dumps(vless.render_config(meta()), sort_keys=True)
        after = json.dumps(vless.render_config(meta(fingerprint="ios")), sort_keys=True)
        self.assertEqual(before, after)

    def test_суффикс_метки(self):
        data = meta()
        url = vless.link(data, data["clients"][0], label_suffix="-safari")
        self.assertTrue(url.endswith("#vlad-iphone-safari"), url)

    def test_fp_links_пять_отпечатков_одна_связка(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(vless.main(common + ["fp-links", "one"]), 0)
            urls = [line for line in out.getvalue().splitlines() if line.startswith("vless://")]
            self.assertEqual(len(urls), 5)
            fps = [linkmod.parse(u)["fp"] for u in urls]
            self.assertEqual(fps, ["chrome", "safari", "firefox", "ios", "randomized"])
            # Всё, кроме отпечатка и метки, одинаково: иначе перебор
            # проверяет не одну переменную, а несколько сразу.
            keys = {(linkmod.parse(u)["id"], linkmod.parse(u)["port"], linkmod.parse(u)["sni"],
                     linkmod.parse(u)["pbk"], linkmod.parse(u)["sid"]) for u in urls}
            self.assertEqual(len(keys), 1)
            self.assertTrue(all(linkmod.parse(u)["name"].endswith(fp) for u, fp in zip(urls, fps)))

    def test_cli_отвергает_неизвестный(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            with self.assertRaises(SystemExit):
                run_cli(*common, "set-fingerprint", "netscape")
            self.assertEqual(run_cli(*common, "set-fingerprint", "firefox"), 0)
            self.assertEqual(vless.load_meta(meta_path)["fingerprint"], "firefox")


class Cdn(unittest.TestCase):
    """Маршрут через CDN.

    Когда DPI режет любой TLS к нашему адресу, человек должен ходить не
    на него, а на адрес Cloudflare. Здесь всё, что можно проверить без
    Cloudflare: вход собирается, порты не сталкиваются, ссылка ведёт на
    домен, а не на IP, и клиент из этой ссылки собирается тем же, что
    сервер ждёт.
    """

    def with_cdn(self, **over):
        cdn = {"domain": "vpn.example.com", "cert": "/c.crt", "key": "/c.key",
               "path": "/abc123", "port": 443, "ws_port": 8443, "ws_path": "/ws456"}
        cdn.update(over)
        return meta(port=8444, cdn=cdn)

    def test_вход_собирается(self):
        config = vless.render_config(self.with_cdn())
        json.dumps(config)
        tags = [i["tag"] for i in config["inbounds"]]
        self.assertIn("vless-cdn", tags)
        cdn = next(i for i in config["inbounds"] if i["tag"] == "vless-cdn")
        self.assertEqual(cdn["port"], 443)
        self.assertEqual(cdn["streamSettings"]["network"], "xhttp")
        self.assertEqual(cdn["streamSettings"]["security"], "tls")
        self.assertEqual(cdn["streamSettings"]["xhttpSettings"]["path"], "/abc123")
        certs = cdn["streamSettings"]["tlsSettings"]["certificates"][0]
        self.assertEqual(certs["certificateFile"], "/c.crt")
        self.assertEqual(certs["keyFile"], "/c.key")

    def test_vision_снят_только_на_cdn_входе(self):
        # WebSocket с Vision несовместим; REALITY-вход его сохраняет.
        config = vless.render_config(self.with_cdn())
        by_tag = {i["tag"]: i for i in config["inbounds"]}
        self.assertNotIn("flow", by_tag["vless-cdn"]["settings"]["clients"][0])
        self.assertEqual(by_tag["vless-reality"]["settings"]["clients"][0]["flow"], vless.FLOW)

    def test_клиенты_общие(self):
        config = vless.render_config(self.with_cdn())
        by_tag = {i["tag"]: i for i in config["inbounds"]}
        self.assertEqual(
            [c["id"] for c in by_tag["vless-cdn"]["settings"]["clients"]],
            [c["id"] for c in by_tag["vless-reality"]["settings"]["clients"]],
        )

    def test_порты_не_сталкиваются(self):
        ports = [i["port"] for i in vless.render_config(self.with_cdn())["inbounds"]]
        self.assertEqual(len(ports), len(set(ports)), ports)

    def test_ссылка_ведёт_на_домен_а_не_на_ip(self):
        data = self.with_cdn()
        url = vless.cdn_link(data, data["clients"][0])
        parsed = urlparse(url)
        self.assertEqual(parsed.hostname, "vpn.example.com")
        self.assertNotIn(data["host"], url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(query["type"], "xhttp")
        self.assertEqual(query["security"], "tls")
        self.assertEqual(query["path"], "/abc123")
        # Через Cloudflare проходит только packet-up.
        self.assertEqual(query["mode"], "packet-up")
        self.assertEqual(query["sni"], "vpn.example.com")
        self.assertEqual(query["host"], "vpn.example.com")
        self.assertNotIn("flow", query)
        self.assertNotIn("pbk", query)
        self.assertTrue(url.endswith("#vlad-iphone-cdn-xhttp"), url)

    def test_ссылка_разбирается_и_даёт_тот_же_клиент(self):
        data = self.with_cdn()
        url = vless.cdn_link(data, data["clients"][0])
        parsed = linkmod.parse(url)
        self.assertEqual(parsed["security"], "tls")
        self.assertEqual(parsed["transport"], "xhttp")
        self.assertEqual(parsed["path"], "/abc123")
        config = linkmod.client_config(url)
        out = config["outbounds"][0]
        self.assertEqual(out["settings"]["vnext"][0]["address"], "vpn.example.com")
        self.assertEqual(out["settings"]["vnext"][0]["port"], 443)
        self.assertNotIn("flow", out["settings"]["vnext"][0]["users"][0])
        self.assertEqual(out["streamSettings"]["network"], "xhttp")
        self.assertEqual(out["streamSettings"]["security"], "tls")
        self.assertEqual(out["streamSettings"]["xhttpSettings"]["path"], "/abc123")
        self.assertEqual(out["streamSettings"]["xhttpSettings"]["host"], "vpn.example.com")
        self.assertEqual(out["streamSettings"]["xhttpSettings"]["mode"], "packet-up")
        self.assertEqual(out["streamSettings"]["tlsSettings"]["serverName"], "vpn.example.com")

    def test_tls_ссылка_без_пути_отвергается(self):
        with self.assertRaises(linkmod.LinkError):
            linkmod.parse("vless://x@vpn.example.com:443?type=xhttp&security=tls")

    def test_старая_ws_ссылка_тоже_разбирается(self):
        # Чужие ссылки бывают и на WebSocket — не отвергать.
        config = linkmod.client_config(
            "vless://x@vpn.example.com:443?type=ws&security=tls&path=%2Fp&host=vpn.example.com"
        )
        self.assertEqual(config["outbounds"][0]["streamSettings"]["network"], "ws")

    def test_reality_ссылки_разбираются_как_раньше(self):
        data = meta()
        parsed = linkmod.parse(vless.link(data, data["clients"][0]))
        self.assertEqual(parsed["security"], "reality")
        self.assertEqual(parsed["transport"], "tcp")

    def test_второй_вход_на_websocket(self):
        # Через бесплатный тариф CDN xhttp проходит не всегда, websocket
        # проходит всегда: поднимаем оба, чтобы не терять круг переписки.
        config = vless.render_config(self.with_cdn())
        ws = next(i for i in config["inbounds"] if i["tag"] == "vless-cdn-ws")
        self.assertEqual(ws["port"], 8443)
        self.assertEqual(ws["streamSettings"]["network"], "ws")
        self.assertEqual(ws["streamSettings"]["security"], "tls")
        self.assertEqual(ws["streamSettings"]["wsSettings"]["path"], "/ws456")
        self.assertNotIn("flow", ws["settings"]["clients"][0])

    def test_оба_входа_на_одном_сертификате_и_клиентах(self):
        config = vless.render_config(self.with_cdn())
        by_tag = {i["tag"]: i for i in config["inbounds"]}
        left = by_tag["vless-cdn"]["streamSettings"]["tlsSettings"]
        right = by_tag["vless-cdn-ws"]["streamSettings"]["tlsSettings"]
        self.assertEqual(left["certificates"], right["certificates"])
        self.assertEqual(
            by_tag["vless-cdn"]["settings"]["clients"],
            by_tag["vless-cdn-ws"]["settings"]["clients"],
        )

    def test_ссылка_websocket(self):
        data = self.with_cdn()
        url = vless.cdn_link(data, data["clients"][0], "ws")
        parsed = urlparse(url)
        self.assertEqual(parsed.hostname, "vpn.example.com")
        self.assertEqual(parsed.port, 8443)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(query["type"], "ws")
        self.assertEqual(query["path"], "/ws456")
        self.assertNotIn("mode", query)
        self.assertTrue(url.endswith("#vlad-iphone-cdn-ws"), url)
        # Клиент из неё собирается тем же, что ждёт сервер.
        stream = linkmod.client_config(url)["outbounds"][0]["streamSettings"]
        self.assertEqual(stream["network"], "ws")
        self.assertEqual(stream["wsSettings"]["path"], "/ws456")

    def test_reality_уступает_оба_порта_cdn(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "8443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            # REALITY стоял ровно на том порту, который забирает websocket.
            self.assertEqual(run_cli(*common, "cdn-setup", "--domain", "d.com",
                                     "--cert", "/c", "--key", "/k", "--path", "/p",
                                     "--ws-port", "8443", "--reality-port", "8444"), 0)
            self.assertEqual(vless.load_meta(meta_path)["port"], 8444)
            written = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            ports = sorted(i["port"] for i in written["inbounds"])
            self.assertEqual(ports, [443, 8443, 8444])

    def test_reality_порт_не_может_совпасть_с_cdn(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            self.assertEqual(run_cli(*common, "cdn-setup", "--domain", "d.com",
                                     "--cert", "/c", "--key", "/k", "--path", "/p",
                                     "--ws-port", "8443", "--reality-port", "8443"), 1)

    def test_cli_setup_уводит_reality_с_занятого_порта(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            self.assertEqual(run_cli(*common, "cdn-setup", "--domain", "d.com",
                                     "--cert", "/c", "--key", "/k", "--path", "/p"), 0)
            saved = vless.load_meta(meta_path)
            self.assertEqual(saved["port"], 8444)
            self.assertEqual(saved["cdn"]["domain"], "d.com")
            written = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertEqual(sorted(i["port"] for i in written["inbounds"]), [443, 8443, 8444])
            # Убрали — REALITY остаётся там, куда его увели: ссылки уже розданы.
            self.assertEqual(run_cli(*common, "cdn-clear"), 0)
            saved = vless.load_meta(meta_path)
            self.assertNotIn("cdn", saved)
            self.assertEqual(saved["port"], 8444)

    def test_cli_setup_не_трогает_порт_если_свободен(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "8500", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            run_cli(*common, "cdn-setup", "--domain", "d.com",
                    "--cert", "/c", "--key", "/k", "--path", "/p")
            self.assertEqual(vless.load_meta(meta_path)["port"], 8500)

    def test_get_вложенное_поле(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            run_cli(*common, "cdn-setup", "--domain", "d.com",
                    "--cert", "/c", "--key", "/k", "--path", "/p")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(vless.main(common + ["get", "cdn.domain"]), 0)
            self.assertEqual(out.getvalue().strip(), "d.com")
            self.assertEqual(run_cli(*common, "get", "cdn.нет"), 1)


class Shadowsocks(unittest.TestCase):
    """Вход Shadowsocks.

    Ставится там, где DPI убивает рукопожатие TLS. Проверяем, что вход
    собирается, порты не сталкиваются, ссылка разбирается обратно в тот
    же метод и пароль, и что VLESS при этом цел.
    """

    def with_ss(self, **over):
        ss = {"port": 443, "method": vless.SS_METHOD, "password": "cGFzc3dvcmQxMjM0NTY3OA==",
              "name": "vpn"}
        ss.update(over)
        return meta(port=8443, ss=ss)

    def test_вход_собирается(self):
        config = vless.render_config(self.with_ss())
        json.dumps(config)
        ss = next(i for i in config["inbounds"] if i["tag"] == "shadowsocks")
        self.assertEqual(ss["port"], 443)
        self.assertEqual(ss["protocol"], "shadowsocks")
        self.assertEqual(ss["settings"]["method"], vless.SS_METHOD)
        self.assertEqual(ss["settings"]["password"], "cGFzc3dvcmQxMjM0NTY3OA==")
        self.assertIn("udp", ss["settings"]["network"])

    def test_метод_из_семейства_2022(self):
        # Старые методы DPI опознаёт по трафику — тогда вход теряет смысл.
        self.assertTrue(vless.SS_METHOD.startswith("2022-blake3-"))

    def test_vless_цел(self):
        config = vless.render_config(self.with_ss())
        reality = next(i for i in config["inbounds"] if i["tag"] == "vless-reality")
        self.assertEqual(reality["settings"]["clients"][0]["flow"], vless.FLOW)
        self.assertEqual(reality["port"], 8443)

    def test_порты_не_сталкиваются(self):
        ports = [i["port"] for i in vless.render_config(self.with_ss())["inbounds"]]
        self.assertEqual(len(ports), len(set(ports)), ports)

    def test_ссылка_разбирается_обратно(self):
        data = self.with_ss()
        url = vless.ss_link(data)
        self.assertTrue(url.startswith("ss://"), url)
        parsed = linkmod.parse(url)
        self.assertEqual(parsed["kind"], "ss")
        self.assertEqual(parsed["host"], data["host"])
        self.assertEqual(parsed["port"], 443)
        self.assertEqual(parsed["method"], vless.SS_METHOD)
        self.assertEqual(parsed["password"], "cGFzc3dvcmQxMjM0NTY3OA==")

    def test_клиент_из_ссылки(self):
        data = self.with_ss()
        config = linkmod.client_config(vless.ss_link(data), 10808)
        out = config["outbounds"][0]
        self.assertEqual(out["protocol"], "shadowsocks")
        server = out["settings"]["servers"][0]
        self.assertEqual(server["address"], data["host"])
        self.assertEqual(server["method"], vless.SS_METHOD)
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")

    def test_ссылка_открытым_текстом_тоже_разбирается(self):
        # Часть клиентов выпускает ss:// без base64 — принимать обе формы.
        parsed = linkmod.parse("ss://2022-blake3-aes-128-gcm:cGFzcw%3D%3D@1.2.3.4:443#x")
        self.assertEqual(parsed["method"], "2022-blake3-aes-128-gcm")
        self.assertEqual(parsed["password"], "cGFzcw==")

    def test_обрезанная_ss_ссылка_отвергается(self):
        for broken in ("ss://@1.2.3.4:443", "ss://bWV0aG9k@1.2.3.4:443"):
            with self.subTest(broken=broken), self.assertRaises(linkmod.LinkError):
                linkmod.parse(broken)

    def test_cli_уводит_reality_с_занятого_порта(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            self.assertEqual(run_cli(*common, "ss-setup", "--port", "443",
                                     "--password", "cGFzc3dvcmQxMjM0NTY3OA=="), 0)
            saved = vless.load_meta(meta_path)
            self.assertEqual(saved["port"], 8443)
            written = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertEqual(sorted(i["port"] for i in written["inbounds"]), [443, 8443])
            self.assertEqual(run_cli(*common, "ss-clear"), 0)
            self.assertNotIn("ss", vless.load_meta(meta_path))

    def test_cli_не_даёт_занять_порт_cdn(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "8500", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            run_cli(*common, "cdn-setup", "--domain", "d.com", "--cert", "/c",
                    "--key", "/k", "--path", "/p")
            self.assertEqual(run_cli(*common, "ss-setup", "--port", "443",
                                     "--password", "cGFzcw=="), 1)


class XhttpTransport(unittest.TestCase):
    """REALITY поверх xhttp.

    Так устроены рабочие узлы известных сервисов: VLESS · xhttp ·
    Reality. Голый TCP с Vision фильтры узнают по рисунку трафика,
    xhttp даёт браузерный рисунок.
    """

    def xhttp(self, **over):
        return meta(network="xhttp", path="/abc123", **over)

    def test_вход_на_xhttp(self):
        config = vless.render_config(self.xhttp())
        inbound = config["inbounds"][0]
        stream = inbound["streamSettings"]
        self.assertEqual(stream["network"], "xhttp")
        self.assertEqual(stream["security"], "reality")
        self.assertEqual(stream["xhttpSettings"]["path"], "/abc123")
        # Ключи и маскировка те же, что на tcp.
        self.assertEqual(stream["realitySettings"]["serverNames"], [meta()["sni"]])

    def test_vision_снимается(self):
        # Vision работает только поверх голого TCP.
        inbound = vless.render_config(self.xhttp())["inbounds"][0]
        self.assertNotIn("flow", inbound["settings"]["clients"][0])

    def test_tcp_по_умолчанию_с_vision(self):
        inbound = vless.render_config(meta())["inbounds"][0]
        self.assertEqual(inbound["streamSettings"]["network"], "tcp")
        self.assertNotIn("xhttpSettings", inbound["streamSettings"])
        self.assertEqual(inbound["settings"]["clients"][0]["flow"], vless.FLOW)

    def test_ссылка_несёт_транспорт_и_путь(self):
        data = self.xhttp()
        url = vless.link(data, data["clients"][0])
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertEqual(query["type"], "xhttp")
        self.assertEqual(query["path"], "/abc123")
        self.assertEqual(query["security"], "reality")
        self.assertEqual(query["pbk"], data["public_key"])
        self.assertNotIn("flow", query)

    def test_клиент_из_ссылки_совпадает_с_сервером(self):
        data = self.xhttp()
        url = vless.link(data, data["clients"][0])
        config = linkmod.client_config(url)
        stream = config["outbounds"][0]["streamSettings"]
        self.assertEqual(stream["network"], "xhttp")
        self.assertEqual(stream["security"], "reality")
        self.assertEqual(stream["xhttpSettings"]["path"], "/abc123")
        user = config["outbounds"][0]["settings"]["vnext"][0]["users"][0]
        self.assertNotIn("flow", user)

    def test_пробные_входы_остаются_на_tcp(self):
        # Они нужны для перебора доменов, а не транспортов: иначе
        # перебор менял бы две переменные разом.
        data = self.xhttp(alts=[{"sni": "www.bing.com", "port": 8443}])
        config = vless.render_config(data)
        self.assertEqual(config["inbounds"][0]["streamSettings"]["network"], "xhttp")
        self.assertEqual(config["inbounds"][1]["streamSettings"]["network"], "tcp")

    def test_cli_переключает_туда_и_обратно(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "h", "--port", "443", "--sni", "s",
                    "--dest", "s:443", "--private-key", "P", "--public-key", "U",
                    "--short-id", "a", "--client", "one")
            self.assertEqual(run_cli(*common, "set-transport", "xhttp", "--path", "/p"), 0)
            saved = vless.load_meta(meta_path)
            self.assertEqual(saved["network"], "xhttp")
            self.assertEqual(saved["path"], "/p")
            written = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertEqual(written["inbounds"][0]["streamSettings"]["network"], "xhttp")
            self.assertEqual(run_cli(*common, "set-transport", "tcp"), 0)
            written = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertEqual(written["inbounds"][0]["streamSettings"]["network"], "tcp")

    def test_reality_ссылка_с_чужим_транспортом_отвергается(self):
        with self.assertRaises(linkmod.LinkError):
            linkmod.parse("vless://x@1.2.3.4:443?type=grpc&security=reality&pbk=K")


class SetHost(unittest.TestCase):
    """Смена адреса в ссылках.

    Когда у оператора заблокирован приём данных на конкретный адрес, не
    помогает ни один протокол — помогает только другой адрес. Xray
    слушает 0.0.0.0, поэтому добавленный у хостера второй IPv4 работает
    сразу: меняются только ссылки.
    """

    def test_ссылки_переезжают(self):
        data = meta(host="203.0.113.10")
        client = data["clients"][0]
        self.assertIn("@203.0.113.10:", vless.link(data, client))
        data["host"] = "198.51.100.7"
        self.assertIn("@198.51.100.7:", vless.link(data, client))
        self.assertNotIn("203.0.113.10", vless.link(data, client))

    def test_конфиг_сервера_не_зависит_от_адреса(self):
        # Поэтому перезапуск службы после смены не нужен.
        before = json.dumps(vless.render_config(meta(host="203.0.113.10")), sort_keys=True)
        after = json.dumps(vless.render_config(meta(host="198.51.100.7")), sort_keys=True)
        self.assertEqual(before, after)

    def test_пробные_и_ss_ссылки_тоже_переезжают(self):
        data = meta(host="198.51.100.7",
                    alts=[{"sni": "www.bing.com", "port": 8443}],
                    ss={"port": 8500, "method": vless.SS_METHOD,
                        "password": "cGFzcw==", "name": "vpn"})
        self.assertIn("@198.51.100.7:8443", vless.link(data, data["clients"][0], data["alts"][0]))
        self.assertIn("@198.51.100.7:8500", vless.ss_link(data))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "203.0.113.10", "--port", "443",
                    "--sni", "s", "--dest", "s:443", "--private-key", "P",
                    "--public-key", "U", "--short-id", "a", "--client", "one")
            self.assertEqual(run_cli(*common, "set-host", "198.51.100.7"), 0)
            self.assertEqual(vless.load_meta(meta_path)["host"], "198.51.100.7")


class DualStack(unittest.TestCase):
    """Двойной стек и ссылки на другой адрес.

    «0.0.0.0» принимает только IPv4; «::» — и IPv6, и IPv4. Разница
    важна: списки блокировок построены вокруг IPv4, а у хостера обычно
    выделен целый блок IPv6, и это бесплатный запас свежих адресов.
    """

    def test_все_входы_слушают_оба_стека(self):
        data = meta(alts=[{"sni": "www.bing.com", "port": 8443}],
                    ss={"port": 8500, "method": vless.SS_METHOD,
                        "password": "cGFzcw==", "name": "vpn"},
                    cdn={"domain": "d.com", "cert": "/c", "key": "/k",
                         "path": "/p", "port": 8446, "ws_port": 8447})
        config = vless.render_config(data)
        self.assertEqual(len(config["inbounds"]), 5)
        for inbound in config["inbounds"]:
            self.assertEqual(inbound["listen"], "::", inbound["tag"])

    def test_ссылка_на_ipv6_в_скобках(self):
        data = meta(host="2a03:4000:56:c81::1")
        url = vless.link(data, data["clients"][0])
        self.assertIn("@[2a03:4000:56:c81::1]:", url)
        self.assertEqual(urlparse(url).hostname, "2a03:4000:56:c81::1")

    def test_links_for_не_трогает_шпаргалку(self):
        # Адрес нужен для пробы; прежние ссылки должны продолжать работать.
        with tempfile.TemporaryDirectory() as directory:
            config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
            common = ["--config", config, "--meta", meta_path]
            run_cli(*common, "init", "--host", "203.0.113.10", "--port", "443",
                    "--sni", "s", "--dest", "s:443", "--private-key", "P",
                    "--public-key", "U", "--short-id", "a", "--client", "one")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(vless.main(common + ["links-for", "2a03:4000:56:c81::1"]), 0)
            printed = out.getvalue()
            self.assertIn("@[2a03:4000:56:c81::1]:443", printed)
            self.assertEqual(vless.load_meta(meta_path)["host"], "203.0.113.10")

    def test_links_for_включает_shadowsocks(self):
        data = meta(ss={"port": 8500, "method": vless.SS_METHOD,
                        "password": "cGFzcw==", "name": "vpn"})
        variant = {**data, "host": "2a03:4000:56:c81::1"}
        self.assertIn("@[2a03:4000:56:c81::1]:8500", vless.ss_link(variant))


class Reset(unittest.TestCase):
    """Возврат к одному чистому входу.

    Во время разбора накапливаются пробные входы, Shadowsocks, CDN,
    сменённый транспорт. Потом это мешает: лишние открытые порты и
    непонятно, какая ссылка от чего.
    """

    def loaded(self, directory):
        config, meta_path = f"{directory}/config.json", f"{directory}/reality.json"
        common = ["--config", config, "--meta", meta_path]
        run_cli(*common, "init", "--host", "203.0.113.10", "--port", "8444",
                "--sni", "dl.google.com", "--dest", "dl.google.com:443",
                "--private-key", "СТАРЫЙ", "--public-key", "СТАРЫЙПУБ",
                "--short-id", "aaaa", "--client", "one")
        run_cli(*common, "set-alts", "www.bing.com:8443", "www.apple.com:8445")
        run_cli(*common, "ss-setup", "--port", "8500", "--password", "cGFzcw==")
        run_cli(*common, "set-transport", "xhttp", "--path", "/old")
        run_cli(*common, "set-fingerprint", "safari")
        run_cli(*common, "add", "two")
        return common, config, meta_path

    def test_остаётся_один_вход(self):
        with tempfile.TemporaryDirectory() as directory:
            common, config, meta_path = self.loaded(directory)
            before = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertGreater(len(before["inbounds"]), 3)
            self.assertEqual(run_cli(*common, "reset", "--private-key", "НОВЫЙ",
                                     "--public-key", "НОВЫЙПУБ", "--short-id", "bbbb"), 0)
            after = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
            self.assertEqual(len(after["inbounds"]), 1)
            self.assertEqual(after["inbounds"][0]["port"], 443)
            self.assertEqual(after["inbounds"][0]["streamSettings"]["network"], "tcp")
            self.assertEqual(after["inbounds"][0]["settings"]["clients"][0]["flow"], vless.FLOW)

    def test_адрес_и_домен_сохраняются(self):
        # Они проверены; перевыпускать их незачем.
        with tempfile.TemporaryDirectory() as directory:
            common, _, meta_path = self.loaded(directory)
            run_cli(*common, "reset", "--private-key", "НОВЫЙ",
                    "--public-key", "НОВЫЙПУБ", "--short-id", "bbbb")
            saved = vless.load_meta(meta_path)
            self.assertEqual(saved["host"], "203.0.113.10")
            self.assertEqual(saved["sni"], "dl.google.com")

    def test_наживное_сбрасывается(self):
        with tempfile.TemporaryDirectory() as directory:
            common, _, meta_path = self.loaded(directory)
            run_cli(*common, "reset", "--private-key", "НОВЫЙ",
                    "--public-key", "НОВЫЙПУБ", "--short-id", "bbbb")
            saved = vless.load_meta(meta_path)
            self.assertEqual(saved["private_key"], "НОВЫЙ")
            self.assertEqual(saved["short_id"], "bbbb")
            for key in ("alts", "ss", "cdn", "network", "path", "fingerprint"):
                self.assertNotIn(key, saved, key)
            self.assertEqual([c["name"] for c in saved["clients"]], ["phone"])


if __name__ == "__main__":
    unittest.main()
