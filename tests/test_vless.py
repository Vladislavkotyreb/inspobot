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
            (full.replace("security=reality", "security=tls"), "REALITY"),
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


if __name__ == "__main__":
    unittest.main()
