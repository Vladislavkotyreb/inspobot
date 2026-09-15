"""Проверки AmneziaWG.

Сам туннель отсюда не проверить — нужны две машины. Что проверяемо:
параметры обфускации не нарушают ограничений протокола, конфиги
сервера и клиента согласованы между собой, и всё, без чего туннель
поднимается и молчит, на месте.
"""

import importlib.util
import pathlib
import random
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("awg_admin", ROOT / "deploy" / "awg_admin.py")
awg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(awg)


def meta(**over):
    base = {
        "host": "203.0.113.10",
        "wan": "eth0",
        "port": 51820,
        "private_key": "privSERVER0000",
        "public_key": "pubSERVER1111",
        "params": awg.make_params(random.Random(1), header_key="hpkTEST5555"),
        "clients": [{"name": "phone", "private_key": "privCLIENT2222",
                     "public_key": "pubCLIENT3333",
                     "preshared_key": "pskSHARED4444", "address": "10.8.0.2"}],
    }
    base.update(over)
    return base


class Params(unittest.TestCase):
    """Параметры обфускации сверены с эталоном Amnezia (протокол 3.1):
    client/core/utils/constants/protocolConstants.h и awgInstaller.cpp
    в amnezia-client. Это то, что раздаёт их собственное приложение.

    Ограничения не наши, а протокола: нарушишь — туннель поднимется и
    будет молчать, без единой ошибки в журнале.
    """

    def params(self, seed=0):
        return awg.make_params(random.Random(seed), header_key="hpkTEST5555")

    def test_сгенерированные_проходят_проверку(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                awg.check_params(self.params(seed))

    def test_заголовки_штатные(self):
        # В 3.1 их не рандомизируют: HeaderProtectionKey шифрует
        # заголовки целиком, а случайные значения сами были бы приметой.
        params = self.params()
        self.assertEqual([params[f"H{n}"] for n in (1, 2, 3, 4)], [1, 2, 3, 4])

    def test_защита_заголовков_есть(self):
        self.assertTrue(self.params()["HeaderProtectionKey"])

    def test_совпадает_с_эталоном_amnezia(self):
        params = self.params()
        self.assertEqual(params["Jmin"], 10)
        self.assertEqual(params["Jmax"], 50)
        self.assertTrue(4 <= params["Jc"] <= 6)
        for key in ("S1", "S2", "S3", "S4"):
            self.assertEqual(params[key], 12, key)
        self.assertEqual(params["ContentPaddingAddition"], "10-100")
        self.assertEqual(params["RekeyAfterTime"], "100-120")
        self.assertEqual(params["RekeyTimeout"], "3-7")
        self.assertEqual(params["RejectAfterTime"], "150-180")
        self.assertEqual(params["KeepaliveTimeout"], "5-15")
        self.assertEqual(params["MaxHandshakeAttempts"], "15-20")
        self.assertEqual(params["RandomTrailers"], "on")
        self.assertEqual(params["DisableCookies"], "on")

    def test_порт_не_штатный_для_wireguard(self):
        # 51820 режут по одному номеру, не заглядывая внутрь.
        self.assertNotEqual(awg.PORT, 51820)
        self.assertEqual(awg.PORT, 55424)

    def test_нарушения_отвергаются(self):
        good = self.params()
        for key, value in (
            ("Jc", 0), ("Jc", 200), ("Jmax", 5), ("S1", 200),
            ("HeaderProtectionKey", ""), ("RandomTrailers", "да"),
            ("DisableCookies", "1"),
        ):
            with self.subTest(key=key, value=value):
                broken = dict(good)
                broken[key] = value
                with self.assertRaises(awg.AwgError):
                    awg.check_params(broken)

    def test_совпадающие_заголовки_отвергаются(self):
        broken = self.params()
        broken["H2"] = broken["H1"]
        with self.assertRaises(awg.AwgError):
            awg.check_params(broken)

    def test_s1_плюс_56_равно_s2_отвергается(self):
        broken = self.params()
        broken["S2"] = broken["S1"] + 56
        with self.assertRaises(awg.AwgError):
            awg.check_params(broken)

    def test_нет_поля_отвергается(self):
        for key in ("H3", "HeaderProtectionKey", "DisableCookies"):
            with self.subTest(key=key):
                broken = self.params()
                del broken[key]
                with self.assertRaises(awg.AwgError):
                    awg.check_params(broken)


class Configs(unittest.TestCase):
    def test_параметры_совпадают_у_сервера_и_клиента(self):
        # Расходятся — туннель не встанет: это часть протокола, а не
        # настройка, и подогнать её на одной стороне нельзя.
        data = meta()
        server = awg.server_config(data)
        client = awg.client_config(data, data["clients"][0])
        for key in awg.PARAM_ORDER:
            line = f"{key} = {data['params'][key]}"
            self.assertIn(line, server, key)
            self.assertIn(line, client, key)

    def test_мусорный_пакет_только_у_клиента(self):
        # Мусор шлёт клиент, серверу он не нужен — так же у Amnezia.
        data = meta()
        self.assertIn("I1 = ", awg.client_config(data, data["clients"][0]))
        self.assertNotIn("I1 = ", awg.server_config(data))

    def test_мусорный_пакет_прикидывается_запросом_к_icloud(self):
        client = awg.client_config(meta(), meta()["clients"][0])
        self.assertIn("icloud", bytes.fromhex(
            awg.SPECIAL_JUNK_1.split("0x")[1].rstrip(">")
        ).decode("latin-1"))

    def test_сервер_знает_каждого_клиента(self):
        data = meta(clients=[
            {"name": "phone", "private_key": "A", "public_key": "PA",
             "preshared_key": "SA", "address": "10.8.0.2"},
            {"name": "mac", "private_key": "B", "public_key": "PB",
             "preshared_key": "SB", "address": "10.8.0.3"},
        ])
        server = awg.server_config(data)
        self.assertEqual(server.count("[Peer]"), 2)
        self.assertIn("PublicKey = PA", server)
        self.assertIn("PublicKey = PB", server)
        self.assertIn("AllowedIPs = 10.8.0.2/32", server)
        self.assertIn("AllowedIPs = 10.8.0.3/32", server)

    def test_общий_ключ_пары_совпадает(self):
        data = meta()
        client = data["clients"][0]
        self.assertIn(f"PresharedKey = {client['preshared_key']}", awg.server_config(data))
        self.assertIn(f"PresharedKey = {client['preshared_key']}", awg.client_config(data, client))

    def test_клиент_знает_куда_идти(self):
        data = meta()
        client = awg.client_config(data, data["clients"][0])
        self.assertIn("Endpoint = 203.0.113.10:51820", client)
        self.assertIn("PublicKey = pubSERVER1111", client)
        # Весь трафик в туннель, иначе это не VPN, а маршрут в подсеть.
        self.assertIn("AllowedIPs = 0.0.0.0/0, ::/0", client)

    def test_приватные_ключи_не_утекают_в_чужой_конфиг(self):
        # Значения подобраны непересекающимися: иначе проверка ловила
        # бы публичный ключ, в котором приватный оказался подстрокой.
        data = meta()
        client = data["clients"][0]
        self.assertNotIn(client["private_key"], awg.server_config(data))
        self.assertNotIn(data["private_key"], awg.client_config(data, client))

    def test_пересылка_и_подмена_адреса_на_месте(self):
        # Без них туннель поднимется, а интернета не будет — самая
        # частая жалоба «подключился, но не работает».
        server = awg.server_config(meta())
        self.assertIn("net.ipv4.ip_forward=1", server)
        self.assertIn("-j MASQUERADE", server)
        self.assertIn("-o eth0", server)
        # Что добавили в PostUp, то должно сниматься в PostDown.
        self.assertEqual(server.count("-A POSTROUTING"), server.count("-D POSTROUTING"))
        self.assertEqual(server.count("-A FORWARD"), server.count("-D FORWARD"))

    def test_keepalive_у_клиента(self):
        # Мобильные сети рвут неактивные соединения через минуту-две.
        self.assertIn("PersistentKeepalive = 25", awg.client_config(meta(), meta()["clients"][0]))

    def test_mtu_задан_обеим_сторонам(self):
        data = meta()
        self.assertIn(f"MTU = {awg.MTU}", awg.server_config(data))
        self.assertIn(f"MTU = {awg.MTU}", awg.client_config(data, data["clients"][0]))

    def test_битые_параметры_валят_сборку_конфига(self):
        broken = meta()
        broken["params"] = dict(broken["params"])
        broken["params"]["H2"] = broken["params"]["H1"]
        with self.assertRaises(awg.AwgError):
            awg.server_config(broken)
        with self.assertRaises(awg.AwgError):
            awg.client_config(broken, broken["clients"][0])


class Clients(unittest.TestCase):
    def test_адреса_не_повторяются(self):
        data = meta(clients=[])
        used = []
        for index in range(20):
            data["clients"].append({
                "name": f"c{index}", "private_key": "x", "public_key": "y",
                "preshared_key": "z", "address": awg.next_address(data),
            })
            used.append(data["clients"][-1]["address"])
        self.assertEqual(len(set(used)), len(used))
        self.assertNotIn("10.8.0.1", used)  # адрес сервера

    def test_освободившийся_адрес_переиспользуется(self):
        data = meta()
        data["clients"].append({"name": "b", "private_key": "x", "public_key": "y",
                                "preshared_key": "z", "address": "10.8.0.3"})
        awg.remove_client(data, "phone")
        self.assertEqual(awg.next_address(data), "10.8.0.2")

    def test_кривое_имя_отклоняется(self):
        for name in ("", "имя", "два слова", "x" * 33, "-начало"):
            with self.subTest(name=name), self.assertRaises(awg.AwgError):
                awg.check_name(name)

    def test_нет_клиента_понятная_ошибка(self):
        with self.assertRaises(awg.AwgError):
            awg.find_client(meta(), "нетакого")
        with self.assertRaises(awg.AwgError):
            awg.remove_client(meta(), "нетакого")


class Files(unittest.TestCase):
    def test_права_на_конфиг(self):
        import os, stat
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/nested/awg0.conf"
            awg.write(path, "текст")
            # В конфиге приватный ключ сервера.
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_битый_конфиг_не_затирает_рабочий(self):
        with tempfile.TemporaryDirectory() as directory:
            conf, meta_path = f"{directory}/awg0.conf", f"{directory}/peers.json"
            awg.apply(meta(), conf, meta_path)
            before = pathlib.Path(conf).read_text(encoding="utf-8")
            broken = meta()
            broken["params"] = dict(broken["params"])
            broken["params"]["S2"] = broken["params"]["S1"] + 56
            with self.assertRaises(awg.AwgError):
                awg.apply(broken, conf, meta_path)
            self.assertEqual(pathlib.Path(conf).read_text(encoding="utf-8"), before)

    def test_нет_файла_понятная_ошибка(self):
        with self.assertRaises(awg.AwgError) as caught:
            awg.load("/nonexistent/peers.json")
        self.assertIn("не установлен", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
