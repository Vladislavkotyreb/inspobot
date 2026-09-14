#!/usr/bin/env sh
# Проверка ссылки настоящим клиентом, с обычного компьютера.
#
#   sh deploy/test-link.sh 'vless://...' 'vless://...'
#
# Ссылок можно дать несколько — проверит каждую и скажет, какая прошла.
#
# Запускать НА СВОЁМ компьютере (мак, линукс), не на сервере. Скачивает
# Xray во временный каталог, поднимает клиента по ссылке, выходит через
# него наружу и показывает адрес. Ничего не устанавливает и после себя
# убирает.
#
# Зачем: сервер, прошедший через собственный туннель по петле, ничего не
# доказывает про внешнее подключение — ни сеть, ни приложение в этом не
# участвовали. Эта проверка участвует обеими.

set -e

[ $# -gt 0 ] || {
    echo "Нужна ссылка в кавычках (можно несколько):" >&2
    echo "    sh deploy/test-link.sh 'vless://...' 'vless://...'" >&2
    exit 1
}

DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_URL="${VLESS_CHECK_URL:-https://api.ipify.org}"
command -v python3 >/dev/null 2>&1 || { echo "Нет python3." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "Нет curl." >&2; exit 1; }

# 1. Какой сборкой качать
OS=$(uname -s)
ARCH=$(uname -m)
case "$OS:$ARCH" in
    Darwin:arm64)        ASSET=Xray-macos-arm64-v8a.zip ;;
    Darwin:x86_64)       ASSET=Xray-macos-64.zip ;;
    Linux:x86_64)        ASSET=Xray-linux-64.zip ;;
    Linux:aarch64|Linux:arm64) ASSET=Xray-linux-arm64-v8a.zip ;;
    *) echo "Не знаю сборки под $OS $ARCH." >&2; exit 1 ;;
esac

WORK=$(mktemp -d)
CLIENT=""
cleanup() {
    [ -n "$CLIENT" ] && kill "$CLIENT" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

echo "Скачиваю Xray ($ASSET)..."
curl -fsSL -o "$WORK/xray.zip" \
    "https://github.com/XTLS/Xray-core/releases/latest/download/$ASSET"
if command -v unzip >/dev/null 2>&1; then
    unzip -oq "$WORK/xray.zip" -d "$WORK"
else
    python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
        "$WORK/xray.zip" "$WORK"
fi
chmod +x "$WORK/xray"
# Скачанный бинарник не подписан: без снятия карантина macOS его не пустит.
[ "$OS" = "Darwin" ] && xattr -dr com.apple.quarantine "$WORK" 2>/dev/null || true
"$WORK/xray" version | head -1

# 2. Прямой выход — ДО туннеля. Если его нет, проверять нечего:
#    значит, на машине уже поднят другой VPN или прокси, и что бы мы
#    дальше ни намерили, это будет про него, а не про ссылку.
DIRECT=$(curl -s -m 15 "$CHECK_URL" 2>/dev/null || true)
if [ -z "$DIRECT" ]; then
    echo "Без туннеля наружу не выходит." >&2
    echo >&2
    echo "Значит, на этой машине уже работает VPN или прокси — скорее всего" >&2
    echo "Happ или другой клиент. Выключите его полностью (не просто" >&2
    echo "отключите соединение, а закройте приложение) и повторите." >&2
    echo "Иначе проверка измеряет его, а не нашу ссылку." >&2
    exit 1
fi

echo "Без туннеля вы выходите с адреса: $DIRECT"

# 3. Свободный порт под каждую попытку. Клиенты VPN занимают 10808 и
#    соседние; сядешь на занятый — в проверку потечёт чужой трафик, а
#    вывод будет выглядеть как приговор ссылке.
free_port() {
    python3 - <<'INNER'
import socket
for port in range(18080, 18200):
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        continue
    finally:
        probe.close()
    print(port)
    break
INNER
}

field() {
    python3 "$DIR/vless_link.py" "$1" --show 2>/dev/null | sed -n "s/^$2 *//p"
}

# 4. По одной ссылке за раз
try_link() {
    ONE="$1"
    SOCKS=$(free_port)
    [ -n "$SOCKS" ] || { echo "нет свободного порта"; return 1; }

    SERVER=$(field "$ONE" host)
    printf '  %-22s порт %-6s ... ' "$(field "$ONE" sni)" "$(field "$ONE" port)"

    if ! python3 "$DIR/vless_link.py" "$ONE" --socks "$SOCKS" > "$WORK/client.json" 2>"$WORK/err"; then
        echo "ссылка не разобралась: $(cat "$WORK/err")"
        return 2
    fi

    "$WORK/xray" run -c "$WORK/client.json" > "$WORK/log" 2>&1 &
    CLIENT=$!
    sleep 2
    if ! kill -0 "$CLIENT" 2>/dev/null; then
        echo "клиент не запустился"
        tail -5 "$WORK/log" | sed 's/^/      /'
        CLIENT=""
        return 1
    fi

    THROUGH=$(curl -s -m 20 --socks5-hostname "127.0.0.1:$SOCKS" "$CHECK_URL" 2>/dev/null || true)

    # Чужой трафик означает, что порт всё-таки с кем-то делится, и
    # результат — не про эту ссылку.
    FOREIGN=$(grep -c 'accepted' "$WORK/log" 2>/dev/null || echo 0)

    kill "$CLIENT" 2>/dev/null
    wait "$CLIENT" 2>/dev/null || true
    CLIENT=""

    if [ "$FOREIGN" -gt 6 ]; then
        echo "порт занят чужим трафиком ($FOREIGN соединений) — закройте VPN-клиент"
        return 1
    fi
    if [ -z "$THROUGH" ]; then
        echo "НЕТ"
        return 1
    fi
    if [ "$THROUGH" = "$SERVER" ]; then
        echo "РАБОТАЕТ (вышли с $THROUGH)"
        return 0
    fi
    echo "мимо: вышли с $THROUGH, а сервер $SERVER"
    return 1
}

echo
echo "=== проверяю ссылки ==="
WORKED=0
TRIED=0
for ONE in "$@"; do
    set +e
    try_link "$ONE"
    CODE=$?
    set -e
    # 2 — ссылку не удалось разобрать: клиент даже не запускался, и
    # такую попытку нельзя считать доводом ни за, ни против сервера.
    [ "$CODE" = "2" ] || TRIED=$((TRIED + 1))
    [ "$CODE" = "0" ] && WORKED=$((WORKED + 1))
done

echo
if [ "$WORKED" -gt 0 ]; then
    echo "Прошло ссылок: $WORKED. Сервер и связка исправны."
    echo "Пришлите строку с пометкой РАБОТАЕТ — переведу основной вход на неё."
elif [ "$TRIED" = "0" ]; then
    echo "Ни одну ссылку не удалось разобрать — проверять было нечего."
    echo "Скорее всего, они обрезались при копировании. Возьмите их заново"
    echo "и не забудьте кавычки вокруг каждой."
else
    echo "Не прошла ни одна из $TRIED, а прямой выход есть ($DIRECT)."
    echo "Значит, дело не в приложении и не в маскировочном домене:"
    echo "соединение к этому серверу режут по дороге."
    if [ -f "$WORK/log" ]; then
        echo
        echo "Последние строки журнала последней попытки:"
        tail -10 "$WORK/log" | sed 's/^/    /'
    fi
fi
