#!/usr/bin/env sh
# Полная картина: что на сервере запущено, что слушает и доходит ли
# до этого из России.
#
#   sudo sh deploy/vpn-status.sh
#
# Ничего не меняет и не перезапускает — только смотрит.

DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/usr/sbin:/sbin:$PATH"

say() { printf '\n=== %s ===\n' "$1"; }

[ "$(id -u)" = "0" ] || { echo "Нужен root: sudo sh deploy/vpn-status.sh" >&2; exit 1; }

say "машина"
echo "  адрес:  $(curl -s -m 10 https://api.ipify.org 2>/dev/null || echo 'не определился')"
echo "  память: $(awk '/MemAvailable/{printf "%d МБ свободно", $2/1024}' /proc/meminfo 2>/dev/null)"
echo "  диск:   $(df -h / 2>/dev/null | awk 'NR==2{print $4 " свободно"}')"
echo "  время:  $(date '+%H:%M:%S %Z')"

say "что слушает наружу"
# Именно наружу: localhost-порты никому за пределами машины не видны,
# и в отчёте только путают.
if command -v ss >/dev/null 2>&1; then
    printf '  %-6s %-22s %s\n' ТИП АДРЕС ПРОЦЕСС
    ss -lntup 2>/dev/null | awk 'NR>1 {
        split($5, a, ":"); port = a[length(a)]; addr = $5
        if (addr ~ /^127\./ || addr ~ /^\[::1\]/) next
        proc = ""; if (match($0, /users:\(\("[^"]+"/)) {
            proc = substr($0, RSTART+8, RLENGTH-8); gsub(/"/, "", proc)
        }
        printf "  %-6s %-22s %s\n", $1, addr, proc
    }' | sort -u
else
    echo "  нет ss — apt install -y iproute2"
fi

say "службы"
for UNIT in xray amneziawg inspobot-listener docker; do
    STATE=$(systemctl is-active "$UNIT" 2>/dev/null || echo "нет")
    printf '  %-20s %s\n' "$UNIT" "$STATE"
done
if command -v docker >/dev/null 2>&1; then
    COUNT=$(docker ps -q 2>/dev/null | wc -l)
    echo "  контейнеров запущено: $COUNT"
    [ "$COUNT" != "0" ] && docker ps --format '    {{.Names}} {{.Ports}}' 2>/dev/null
fi

say "Xray"
if [ -f /usr/local/etc/xray/config.json ]; then
    python3 - <<'INNER' 2>/dev/null || echo "  конфиг не разобрался"
import json
data = json.load(open("/usr/local/etc/xray/config.json", encoding="utf-8"))
for inbound in data.get("inbounds", []):
    stream = inbound.get("streamSettings", {})
    reality = stream.get("realitySettings", {})
    extra = reality.get("serverNames", [""])[0] if reality else stream.get("security", "")
    print(f"  {inbound.get('tag','?'):<18} порт {inbound.get('port'):<6} "
          f"{stream.get('network','?')}/{stream.get('security','?')} {extra}")
INNER
else
    echo "  не установлен"
fi

say "AmneziaWG"
if [ -f /etc/amnezia/amneziawg/awg0.conf ]; then
    PORT=$(sed -n 's/^ListenPort *= *//p' /etc/amnezia/amneziawg/awg0.conf | head -1)
    echo "  конфиг есть, порт ${PORT:-?}/udp"
    awg show awg0 2>/dev/null | sed -n '1,3p' | sed 's/^/  /' || echo "  интерфейс не поднят"
    PEERS=$(awg show awg0 2>/dev/null | grep -c '^peer:')
    echo "  клиентов: $PEERS"
    HANDSHAKE=$(awg show awg0 latest-handshakes 2>/dev/null | awk 'NF == 2 && $2 + 0 > 0' | wc -l)
    echo "  было рукопожатий: $HANDSHAKE"
else
    echo "  наш не установлен"
fi

say "установка приложением Amnezia"
if ! command -v docker >/dev/null 2>&1 || [ -z "$(docker ps -q 2>/dev/null)" ]; then
    echo "  контейнеров нет"
else
    # Приложение ставит своё хозяйство в контейнеры, и счётчики
    # рукопожатий живут там. Наш счётчик снаружи к ним отношения не
    # имеет: это разные туннели на разных портах.
    for NAME in $(docker ps --format '{{.Names}}' 2>/dev/null | grep '^amnezia'); do
        echo "  --- $NAME ---"
        docker port "$NAME" 2>/dev/null | sed 's/^/      /' || true
        FOUND=0
        for TOOL in awg wg; do
            OUT=$(docker exec "$NAME" "$TOOL" show 2>/dev/null | grep -v 'OCI runtime')
            [ -z "$OUT" ] && continue
            FOUND=1
            printf '%s\n' "$OUT" | sed -n '1,4p' | sed 's/^/      /'
            PEERS=$(printf '%s\n' "$OUT" | grep -c '^peer:')
            echo "      клиентов: $PEERS"
            # Строго три поля «интерфейс пир время»: иначе в счёт
            # попадает строка с ошибкой, и получается «рукопожатие»
            # там, где туннеля нет вовсе.
            SHAKES=$(docker exec "$NAME" "$TOOL" show all latest-handshakes 2>/dev/null \
                | awk 'NF == 3 && $3 + 0 > 0' | wc -l)
            echo "      было рукопожатий: $SHAKES"
            break
        done
        [ "$FOUND" = "0" ] && echo "      (туннеля нет — это не WireGuard-контейнер)"
    done
    echo
    echo "  «было рукопожатий» больше нуля означает, что пакеты от клиента"
    echo "  доходили: UDP у его оператора проходит. Ноль после попыток —"
    echo "  не доходили вовсе."
fi

say "пересылка и NAT"
echo "  ip_forward: $(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)"
NAT=$(iptables -t nat -S POSTROUTING 2>/dev/null | grep MASQUERADE || true)
if [ -n "$NAT" ]; then printf '%s\n' "$NAT" | sed 's/^/  /'; else echo "  правил MASQUERADE нет"; fi

say "доходит ли до портов из России"
HOST=$(curl -s -m 10 https://api.ipify.org 2>/dev/null)
if [ -z "$HOST" ]; then
    echo "  адрес не определился — пропускаю"
elif [ ! -f "$DIR/vless_reach.py" ]; then
    echo "  нет vless_reach.py"
else
    PORTS=$(ss -lnt 2>/dev/null | awk 'NR>1 {split($4,a,":"); p=a[length(a)]; if (p+0>0) print p}' | sort -un | head -4)
    for P in $PORTS; do
        echo "  --- порт $P/tcp ---"
        python3 "$DIR/vless_reach.py" --host "$HOST" --port "$P" 2>&1 | sed 's/^/    /' | tail -6
    done
    echo
    echo "  Проверка стучится только по TCP: UDP так не проверить."
    echo "  Для AmneziaWG признак связи — строка «было рукопожатий» выше:"
    echo "  если там 0 после попыток подключения, пакеты не дошли."
fi

say "пересечения"
BUSY=$(ss -lnt 2>/dev/null | grep -c ':443 ')
if [ -n "$(docker ps -q 2>/dev/null)" ] && [ -f /usr/local/etc/xray/config.json ]; then
    echo "  На машине и наша установка, и установка приложением Amnezia."
    echo "  Они делят порты и память: 443 занял контейнер, наш Xray ушёл"
    echo "  на запасные порты. Держать обе разом не нужно — лишние"
    echo "  открытые порты и лишний расход памяти."
else
    echo "  установка одна, пересечений нет"
fi

say "вывод"
echo "Если порты слушаются и из России до них доходят, а клиент всё равно"
echo "не подключается — режут не порт, а передачу данных к этому адресу."
echo "Тогда помогает не настройка, а другой адрес или маршрут через CDN."
