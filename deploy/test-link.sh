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

# 2. Конфиг из ссылки
SOCKS=10808
python3 "$DIR/vless_link.py" "$LINK" --socks "$SOCKS" > "$WORK/client.json"
echo
echo "Ссылка разобрана:"
python3 "$DIR/vless_link.py" "$LINK" --show | sed 's/^/    /'

# 3. Проход
echo
echo "Поднимаю клиента и иду наружу..."
"$WORK/xray" run -c "$WORK/client.json" > "$WORK/log" 2>&1 &
CLIENT=$!
sleep 3
if ! kill -0 "$CLIENT" 2>/dev/null; then
    echo "Клиент не запустился:" >&2
    cat "$WORK/log" >&2
    exit 1
fi

DIRECT=$(curl -s -m 15 "$CHECK_URL" 2>/dev/null || true)
THROUGH=$(curl -s -m 25 --socks5-hostname "127.0.0.1:$SOCKS" "$CHECK_URL" 2>/dev/null || true)
SERVER=$(python3 "$DIR/vless_link.py" "$LINK" --show | sed -n 's/^host *//p')

echo
if [ -z "$THROUGH" ]; then
    echo "НЕ ПРОШЛО — туннель не поднялся."
    echo
    echo "Журнал клиента:"
    sed 's/^/    /' "$WORK/log"
    echo
    echo "Свой адрес без туннеля: ${DIRECT:-не определился}"
    echo "Значит, дело не в приложении на телефоне: не работает сама связка."
    exit 1
fi

echo "Без туннеля вы выходите с адреса: ${DIRECT:-не определился}"
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
