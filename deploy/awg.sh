#!/usr/bin/env sh
# AmneziaWG — VPN поверх UDP. Запускать от root:
#
#   sudo sh deploy/awg.sh install [имя]   поставить и завести клиента
#   sudo sh deploy/awg.sh add имя         ещё клиент — конфиг и QR
#   sudo sh deploy/awg.sh config имя      показать конфиг клиента снова
#   sudo sh deploy/awg.sh list            кто заведён
#   sudo sh deploy/awg.sh remove имя      отобрать доступ
#   sudo sh deploy/awg.sh status          что со службой
#   sudo sh deploy/awg.sh uninstall       снести
#
# Зачем, когда есть VLESS: там всё построено на TLS, и если у оператора
# режут TLS к адресу сервера, не помогает ни один из вариантов. Здесь
# UDP и своя обфускация — другая поверхность целиком.
#
# Бота и Xray не трогает: у тех TCP-порты, здесь UDP.

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
ADMIN="$DIR/awg_admin.py"
CONF=/etc/amnezia/amneziawg/awg0.conf
META=/etc/amnezia/amneziawg/peers.json
IFACE=awg0
# 55424 — как у Amnezia: 51820 режут по номеру, это штатный порт WireGuard.
PORT="${AWG_PORT:-55424}"
export PATH="/usr/sbin:/sbin:$PATH"
# awg-quick сам ищет ядерный модуль; его на обычном VPS нет, и без этой
# переменной он ругается и останавливается вместо того, чтобы взять
# userspace-реализацию.
export WG_QUICK_USERSPACE_IMPLEMENTATION=amneziawg-go

die() { echo "$@" >&2; exit 1; }
need_root() { [ "$(id -u)" = "0" ] || die "Нужен root: sudo sh deploy/awg.sh $1"; }
need_installed() { [ -f "$META" ] || die "Не установлено. Сначала: sudo sh deploy/awg.sh install"; }
py() { python3 "$ADMIN" --meta "$META" --conf "$CONF" "$@"; }

show_config() {
    NAME="$1"
    TEXT=$(py config "$NAME")
    echo
    echo "--- конфиг для «$NAME» ---"
    printf '%s\n' "$TEXT"
    echo "--- конец ---"
    if command -v qrencode >/dev/null 2>&1; then
        echo
        printf '%s' "$TEXT" | qrencode -t ansiutf8 -m 2 2>/dev/null || true
        echo "QR не сканируется — сохраните текст выше в файл с именем"
        echo "$NAME.conf и отправьте файлом: приложения импортируют его."
    fi
}

restart_awg() {
    awg-quick down "$IFACE" >/dev/null 2>&1 || true
    awg-quick up "$IFACE" >/dev/null 2>&1 || {
        echo "Интерфейс не поднялся. Что говорит awg-quick:" >&2
        awg-quick up "$IFACE" 2>&1 | tail -15 >&2
        exit 1
    }
}

do_install() {
    need_root install
    [ -f "$META" ] && { echo "Уже установлено. Добавить клиента: sudo sh deploy/awg.sh add имя"; do_status; exit 0; }

    echo "=== пакеты ==="
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git make gcc golang-go iproute2 iptables qrencode curl >/dev/null
    command -v go >/dev/null 2>&1 || die "Go не установился — без него amneziawg-go не собрать."
    echo "go, git, make, iptables, qrencode — на месте."

    echo
    echo "=== сборка AmneziaWG ==="
    BUILD=$(mktemp -d)
    # Собираем из исходников: готовых пакетов для Debian нет, а ядерный
    # модуль требует заголовков ядра и DKMS — на VPS это лишний риск.
    # Userspace-реализация работает на любом ядре.
    git clone --depth 1 -q https://github.com/amnezia-vpn/amneziawg-go.git "$BUILD/go-impl"
    git clone --depth 1 -q https://github.com/amnezia-vpn/amneziawg-tools.git "$BUILD/tools"
    (cd "$BUILD/go-impl" && make >/dev/null 2>&1) || { rm -rf "$BUILD"; die "amneziawg-go не собрался."; }
    install -m755 "$BUILD/go-impl/amneziawg-go" /usr/bin/amneziawg-go
    (cd "$BUILD/tools/src" && make >/dev/null 2>&1 && make install >/dev/null 2>&1) \
        || { rm -rf "$BUILD"; die "amneziawg-tools не собрались."; }
    rm -rf "$BUILD"
    command -v awg >/dev/null 2>&1 || die "awg не установился."
    echo "$(awg --version 2>&1 | head -1)"

    echo
    echo "=== настройка ==="
    HOST="${AWG_HOST:-}"
    [ -n "$HOST" ] || HOST=$(curl -s -m 10 https://api.ipify.org || true)
    [ -n "$HOST" ] || die "Не определил адрес. Задайте: AWG_HOST=1.2.3.4 sudo -E sh deploy/awg.sh install"
    WAN=$(ip -o -4 route show default | awk '{print $5}' | head -1)
    [ -n "$WAN" ] || die "Не нашёл внешний интерфейс."
    echo "адрес: $HOST, интерфейс: $WAN, порт: $PORT/udp"

    NAME="${1:-phone}"
    py init --host "$HOST" --wan "$WAN" --port "$PORT" --client "$NAME" >/dev/null

    # Пересылка нужна и между перезагрузками, не только в PostUp.
    echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-amneziawg.conf
    sysctl -p /etc/sysctl.d/99-amneziawg.conf >/dev/null 2>&1 || true

    cat > /etc/systemd/system/amneziawg.service <<UNIT
[Unit]
Description=AmneziaWG via awg-quick
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=amneziawg-go
ExecStart=/usr/bin/awg-quick up $IFACE
ExecStop=/usr/bin/awg-quick down $IFACE

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable amneziawg >/dev/null 2>&1 || true
    restart_awg

    echo
    echo "=== готово ==="
    do_status
    show_config "$NAME"
    echo
    echo "Приложение на телефон: Amnezia VPN (amnezia.org) или AmneziaWG."
    echo "Обычные клиенты WireGuard не подойдут — обфускацию они не умеют."
    echo
    echo "Ещё клиент: sudo sh deploy/awg.sh add имя"
}

do_add() {
    need_root "add $1"; need_installed
    [ -n "$1" ] || die "Кому? sudo sh deploy/awg.sh add masha"
    py add "$1" >/dev/null
    restart_awg
    show_config "$1"
}

do_remove() {
    need_root "remove $1"; need_installed
    [ -n "$1" ] || die "Кого? sudo sh deploy/awg.sh remove masha"
    py remove "$1"
    restart_awg
    echo "Доступ отобран."
}

do_status() {
    need_root status; need_installed
    echo "служба:  $(systemctl is-active amneziawg 2>/dev/null || echo нет)"
    py show
    if command -v ss >/dev/null 2>&1; then
        ss -lnup 2>/dev/null | grep -q ":$PORT " \
            && echo "порт $PORT/udp: слушается" || echo "порт $PORT/udp: НЕ слушается"
    fi
    awg show "$IFACE" 2>/dev/null | sed -n '1,3p' | sed 's/^/  /' || true
}

do_uninstall() {
    need_root uninstall
    printf 'Снести AmneziaWG и всех его клиентов? Бот и Xray не пострадают. [y/N] '
    read -r ANSWER
    case "$ANSWER" in y|Y|yes|да) ;; *) echo "Отменено."; exit 0 ;; esac
    systemctl disable --now amneziawg >/dev/null 2>&1 || true
    awg-quick down "$IFACE" >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/amneziawg.service /etc/sysctl.d/99-amneziawg.conf
    rm -rf /etc/amnezia
    systemctl daemon-reload
    echo "Снесено. Бинарники /usr/bin/amneziawg-go и awg оставлены."
}

COMMAND="${1:-}"
[ $# -gt 0 ] && shift || true
case "$COMMAND" in
    install)   do_install "$@" ;;
    add)       do_add "${1:-}" ;;
    remove)    do_remove "${1:-}" ;;
    config)    need_root config; need_installed; show_config "${1:-}" ;;
    list)      need_root list; need_installed; py names ;;
    status)    do_status ;;
    uninstall) do_uninstall ;;
    *)         awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0" ;;
esac
