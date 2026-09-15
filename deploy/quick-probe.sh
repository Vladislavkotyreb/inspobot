#!/usr/bin/env sh
# Быстрая проба нового сервера: годится ли его адрес под VPN.
#
# На свежем Debian или Ubuntu, от root, одной командой:
#
#   curl -fsSL https://raw.githubusercontent.com/Vladislavkotyreb/inspobot/claude/inspobot-mobbin-msp-v2p3u1/deploy/quick-probe.sh | sh
#
# Смысл: у Vultr и DigitalOcean оплата почасовая, и проверка адреса
# стоит центы. Не подошёл — снесли, взяли другую локацию. Дешевле
# один раз проверить пять адресов, чем месяц платить за негодный.
#
# Главное здесь — последний шаг: полный запрос по HTTPS с российских
# узлов. Проверка «доходит ли TCP» бесполезна, до заблокированного
# адреса TCP доходит прекрасно; режут передачу данных.
#
# Ничего лишнего не ставит и в систему не прописывается: Xray, конфиг,
# запуск. Сносится строкой в конце вывода.

set -e

PORT=443
DOMAINS="dl.google.com www.bing.com www.samsung.com www.apple.com addons.mozilla.org www.asus.com"
WORK=/tmp/quick-probe
CONF=/usr/local/etc/xray/config.json

die() { echo "$@" >&2; exit 1; }
[ "$(id -u)" = "0" ] || die "Нужен root."
command -v python3 >/dev/null 2>&1 || die "Нет python3."

echo "=== 1/5 пакеты ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq curl openssl qrencode ca-certificates unzip iproute2 >/dev/null 2>&1 || true
mkdir -p "$WORK"

HOST="${PROBE_HOST:-$(curl -s -m 10 https://api.ipify.org || true)}"
[ -n "$HOST" ] || die "Не определил внешний адрес. Задайте: PROBE_HOST=1.2.3.4"
echo "адрес: $HOST"

echo
echo "=== 2/4 маскировочный домен ==="
# Ответ цели обязан уложиться в 8192 байта — столько отведено под него
# в Xray. Домен может быть безупречно доступен и при этом непригоден.
SNI=$(python3 - $DOMAINS <<'INNER'
import socket, ssl, sys
best = None
for host in sys.argv[1:]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2"])
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    handshake = ctx.wrap_bio(inc, out, server_hostname=host)
    total = 0
    try:
        with socket.create_connection((host, 443), timeout=8) as raw:
            raw.settimeout(8)
            while True:
                try:
                    handshake.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    pending = out.read()
                    if pending:
                        raw.sendall(pending)
                    chunk = raw.recv(16384)
                    if not chunk:
                        raise ssl.SSLError("закрыто")
                    total += len(chunk)
                    inc.write(chunk)
            if handshake.version() == "TLSv1.3" and total <= 8192:
                print(f"  {host}: {total} Б", file=sys.stderr)
                if best is None or total < best[1]:
                    best = (host, total)
    except Exception as error:
        print(f"  {host}: не отвечает", file=sys.stderr)
print(best[0] if best else "")
INNER
)
[ -n "$SNI" ] || die "Ни один домен не подошёл."
echo "выбран: $SNI"

echo
echo "=== 3/4 Xray ==="
if ! command -v xray >/dev/null 2>&1; then
    curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh \
        | bash -s -- install >/dev/null 2>&1 || die "Xray не установился."
fi
xray version 2>/dev/null | head -1

KEYS=$(xray x25519)
PRIVATE=$(printf '%s\n' "$KEYS" | grep -i 'private' | head -1 | sed 's/.*[:=][[:space:]]*//')
PUBLIC=$(printf '%s\n' "$KEYS" | grep -iE 'public|password' | head -1 | sed 's/.*[:=][[:space:]]*//')
[ -n "$PRIVATE" ] && [ -n "$PUBLIC" ] || die "Ключи не разобрались."
SHORT=$(openssl rand -hex 8)
UUID=$(xray uuid)

mkdir -p /usr/local/etc/xray
python3 - "$CONF" "$PORT" "$SNI" "$PRIVATE" "$SHORT" "$UUID" <<'INNER'
import json, sys
path, port, sni, private, short, uuid = sys.argv[1:7]
json.dump({
    "log": {"loglevel": "warning", "access": "none"},
    "inbounds": [{
        "tag": "probe", "listen": "::", "port": int(port), "protocol": "vless",
        "settings": {"clients": [{"id": uuid, "flow": "xtls-rprx-vision"}],
                     "decryption": "none"},
        "streamSettings": {"network": "tcp", "security": "reality",
            "realitySettings": {"show": False, "dest": f"{sni}:443", "xver": 0,
                "serverNames": [sni], "privateKey": private, "shortIds": [short]}},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"],
                     "routeOnly": True},
    }],
    "outbounds": [{"protocol": "freedom"}],
}, open(path, "w"), ensure_ascii=False, indent=2)
INNER
# Демон работает не от root: закрытый в 0600 конфиг он не прочитает.
XUSER=$(sed -n 's/^User=//p' /etc/systemd/system/xray.service 2>/dev/null | head -1)
chown "root:$(id -gn "${XUSER:-nobody}" 2>/dev/null || echo nogroup)" "$CONF" 2>/dev/null || true
chmod 0640 "$CONF"; chmod 0755 /usr/local/etc/xray
systemctl enable --now xray >/dev/null 2>&1 || true
systemctl restart xray
sleep 2
systemctl is-active --quiet xray || { journalctl -u xray -n 15 --no-pager; die "Xray не поднялся."; }
ss -lnt 2>/dev/null | grep -q ":$PORT " && echo "порт $PORT слушается"

LINK="vless://$UUID@$HOST:$PORT?type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision&pbk=$PUBLIC&fp=chrome&sni=$SNI&sid=$SHORT&spx=%2F#proba"

echo
echo "=== 4/4 проверка из России ==="
# Проверка по TCP бесполезна: до заблокированного адреса пакеты
# доходят прекрасно, и она показывает «ok» на мёртвом сервере. Режут
# не соединение, а передачу данных, поэтому проверяем полным запросом
# по HTTPS — рукопожатие TLS и обмен данными. Запрос без ключа упрётся
# в запасной ход REALITY и получит настоящий сайт: значит, данные
# ходят.
python3 - "$HOST" "$PORT" <<'INNER' || true
import json, sys, time, urllib.parse, urllib.request

host, port = sys.argv[1], sys.argv[2]

def get(url):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "quick-probe"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode())

try:
    target = urllib.parse.quote(f"https://{host}:{port}/", safe="")
    started = get(f"https://check-host.net/check-http?host={target}&max_nodes=25")
    nodes = started.get("nodes") or {}
    results = {}
    for _ in range(8):
        time.sleep(3)
        results = get(f"https://check-host.net/check-result/{started['request_id']}")
        if results and all(value is not None for value in results.values()):
            break
    rows, ok = [], 0
    for name, info in sorted(nodes.items()):
        if not (isinstance(info, list) and info and str(info[0]).lower() == "ru"):
            continue
        city = str(info[2]) if len(info) > 2 and info[2] else name
        result = results.get(name)
        good = (isinstance(result, list) and result and isinstance(result[0], list)
                and result[0] and result[0][0] == 1)
        if good:
            ok += 1
            rows.append(f"  ok   {city}")
        else:
            why = ""
            if isinstance(result, list) and result and isinstance(result[0], list) \
                    and len(result[0]) > 2 and result[0][2]:
                why = f": {str(result[0][2])[:40]}"
            rows.append(f"  нет  {city}{why}")
    print("\n".join(rows) or "  российских узлов не досталось")
    if rows:
        print(f"  обменялись данными: {ok} из {len(rows)}")
        print()
        if ok == 0:
            print("  АДРЕС НЕ ГОДИТСЯ. Соединение устанавливается, а данные режут —")
            print("  то же самое будет и у живого человека. Сносите и берите другой.")
        else:
            print("  Адрес живой: данные по TLS из России ходят.")
except Exception as error:
    print(f"  проверить не удалось: {type(error).__name__} — проверьте вручную")
INNER

echo
echo "=== ссылка ==="
echo
echo "$LINK"
echo
command -v qrencode >/dev/null 2>&1 && qrencode -t ansiutf8 -m 2 "$LINK" 2>/dev/null || true
cat <<TEXT

Если проверка выше сказала «адрес живой» — отправьте ссылку тому,
кто в России. Если «не годится» — ссылку можно не отправлять, сносите
сервер и берите другой адрес.

  Подключилось     — адрес живой, сервер можно оставлять.
  Не подключилось  — сносите сервер и берите другой адрес.

Смотреть, доходят ли его попытки (в другом окне):

    journalctl -u xray -f | grep -i reality

  строки REALITY     — ClientHello доходит, дело в настройке;
  «received request» — туннель поднялся;
  тишина             — до сервера не дошло ничего, адрес не годится.

Снести пробу целиком:

    bash -c "\$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ remove --purge
TEXT
