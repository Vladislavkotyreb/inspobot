#!/usr/bin/env sh
# Проверка ссылки настоящим клиентом, с обычного компьютера.
#
#   sh deploy/test-link.sh 'vless://...'
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

LINK="${1:-}"
[ -n "$LINK" ] || {
    echo "Нужна ссылка в кавычках:" >&2
    echo "    sh deploy/test-link.sh 'vless://...'" >&2
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

# 3. Свободный порт. Клиенты VPN занимают 10808 и соседние, и если
#    сесть на занятый, в проверку потечёт чужой трафик — а вывод будет
#    выглядеть как приговор ссылке.
SOCKS=$(python3 - <<'INNER'
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
)
[ -n "$SOCKS" ] || { echo "Не нашёл свободного порта." >&2; exit 1; }

python3 "$DIR/vless_link.py" "$LINK" --socks "$SOCKS" > "$WORK/client.json"
echo
echo "Ссылка разобрана:"
python3 "$DIR/vless_link.py" "$LINK" --show | sed 's/^/    /'

# 3. Проход
echo
echo "Поднимаю клиента на порту $SOCKS и иду наружу..."
"$WORK/xray" run -c "$WORK/client.json" > "$WORK/log" 2>&1 &
CLIENT=$!
sleep 3
if ! kill -0 "$CLIENT" 2>/dev/null; then
    echo "Клиент не запустился:" >&2
    tail -20 "$WORK/log" >&2
    exit 1
fi

SERVER=$(python3 "$DIR/vless_link.py" "$LINK" --show | sed -n 's/^host *//p')
THROUGH=$(curl -s -m 25 --socks5-hostname "127.0.0.1:$SOCKS" "$CHECK_URL" 2>/dev/null || true)

# Чужой трафик в нашем прокси означает, что порт всё-таки с кем-то
# делится, и вывод ниже — не про ссылку.
FOREIGN=$(grep -c 'accepted\|rejected' "$WORK/log" 2>/dev/null || echo 0)
if [ "$FOREIGN" -gt 12 ]; then
    echo
    echo "ВНИМАНИЕ: через проверку прошло $FOREIGN соединений вместо одного."
    echo "Значит, порт $SOCKS делится с другим приложением — обычно это"
    echo "работающий VPN-клиент. Закройте его полностью и повторите:"
    echo "результат ниже не про вашу ссылку."
    echo
fi

echo
if [ -z "$THROUGH" ]; then
    echo "НЕ ПРОШЛО — туннель не поднялся."
    echo
    echo "Последние строки журнала клиента:"
    tail -20 "$WORK/log" | sed 's/^/    /'
    echo
    echo "Без туннеля вы выходите с адреса: $DIRECT"
    exit 1
fi

echo "Без туннеля вы выходите с адреса: $DIRECT"
echo "Через туннель — с адреса:         $THROUGH"
echo
if [ "$THROUGH" = "$SERVER" ]; then
    echo "РАБОТАЕТ. Трафик идёт через сервер $SERVER."
    echo "Значит, сервер и ссылка исправны, и если у кого-то не"
    echo "подключается — дело в его приложении или его провайдере."
else
    echo "Странно: вышли не с адреса сервера ($SERVER). Похоже, трафик пошёл мимо."
    exit 1
fi
