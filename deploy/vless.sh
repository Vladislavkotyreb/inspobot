#!/usr/bin/env sh
# VLESS + Reality на этом же сервере. Запускать от root:
#
#   sudo sh deploy/vless.sh install        поставить и завести первого клиента
#   sudo sh deploy/vless.sh add имя        ещё клиент — ссылка и QR
#   sudo sh deploy/vless.sh link имя       показать ссылку снова
#   sudo sh deploy/vless.sh list           все клиенты
#   sudo sh deploy/vless.sh remove имя     отобрать доступ
#   sudo sh deploy/vless.sh status         что с сервером
#   sudo sh deploy/vless.sh check          разобраться, почему не подключается
#   sudo sh deploy/vless.sh repair         пересобрать конфиг и починить права
#   sudo sh deploy/vless.sh uninstall      снести Xray
#
# Бота не трогает: тот никаких портов не слушает, только сам ходит
# наружу. Xray встаёт на 443/tcp, рассылка и кнопки продолжают работать.

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
ADMIN="$DIR/vless_admin.py"
CONFIG=/usr/local/etc/xray/config.json
META=/usr/local/etc/xray/reality.json
PORT="${VLESS_PORT:-443}"

# Маскировочные домены: Reality притворяется трафиком к одному из них.
# Годится тот, что отвечает TLS 1.3 с HTTP/2, не заблокирован в России и
# живёт недалеко от сервера. Проверяются по очереди, берётся первый
# рабочий. Свой вариант: VLESS_SNI=example.com sh deploy/vless.sh install
SNI_CANDIDATES="${VLESS_SNI:-www.microsoft.com dl.google.com www.samsung.com www.asus.com www.nvidia.com www.apple.com}"

die() { echo "$@" >&2; exit 1; }

need_root() {
    [ "$(id -u)" = "0" ] || die "Нужен root: sudo sh deploy/vless.sh $1"
}

need_installed() {
    [ -f "$META" ] || die "Сервер ещё не установлен. Сначала: sudo sh deploy/vless.sh install"
}

py() {
    command -v python3 >/dev/null 2>&1 || die "Нет python3 — поставьте: apt install -y python3"
    python3 "$ADMIN" --config "$CONFIG" --meta "$META" "$@"
}

# Xray работает от nobody, а не от root: конфиг с приватным ключом,
# закрытый в 0600, он открыть не может и падает с permission denied.
# Права проставляет vless_admin.py; здесь — проверка, что вышло.
check_readable() {
    [ -f "$CONFIG" ] || return 0
    XUSER=$(sed -n 's/^User=//p' /etc/systemd/system/xray.service 2>/dev/null | head -1)
    XUSER="${XUSER:-nobody}"
    command -v runuser >/dev/null 2>&1 || return 0
    runuser -u "$XUSER" -- test -r "$CONFIG" 2>/dev/null && return 0
    echo "Конфиг не читается пользователем $XUSER, под которым работает Xray." >&2
    ls -l "$CONFIG" >&2
    echo "Починить: sudo sh deploy/vless.sh repair" >&2
    return 1
}

restart_xray() {
    check_readable || exit 1
    systemctl restart xray
    sleep 1
    systemctl is-active --quiet xray || {
        echo "Xray не поднялся. Что в журнале:" >&2
        journalctl -u xray -n 20 --no-pager >&2 || true
        exit 1
    }
}

show_link() {
    LINK="$1"
    echo
    echo "$LINK"
    echo
    if command -v qrencode >/dev/null 2>&1; then
        qrencode -t ansiutf8 -m 2 "$LINK" || true
        echo "QR не сканируется — просто скопируйте ссылку выше, её понимают все клиенты."
    fi
}

# --- установка -------------------------------------------------------

do_install() {
    need_root install
    if [ -f "$META" ]; then
        echo "Уже установлено. Добавить клиента: sudo sh deploy/vless.sh add имя"
        echo "Если служба не поднимается: sudo sh deploy/vless.sh repair"
        do_status
        exit 0
    fi

    # 1. Порт. Занят — значит на нём уже что-то важное; молча отбирать нельзя.
    if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | grep -q ":$PORT "; then
        echo "Порт $PORT уже занят:" >&2
        ss -lntp 2>/dev/null | grep ":$PORT " >&2 || true
        die "Освободите его или задайте другой: VLESS_PORT=8443 sudo -E sh deploy/vless.sh install"
    fi

    # 2. Инструменты
    echo "=== пакеты ==="
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq curl openssl qrencode ca-certificates unzip >/dev/null
    echo "curl, openssl, qrencode — на месте."

    # 3. Внешний адрес
    HOST="${VLESS_HOST:-}"
    if [ -z "$HOST" ]; then
        HOST=$(curl -s -m 10 https://api.ipify.org || true)
    fi
    [ -n "$HOST" ] || die "Не определил внешний адрес. Задайте руками: VLESS_HOST=1.2.3.4 sudo -E sh deploy/vless.sh install"
    echo "Адрес сервера: $HOST"

    # 4. Маскировочный домен
    echo
    echo "=== маскировка ==="
    SNI=""
    for CANDIDATE in $SNI_CANDIDATES; do
        printf '%-20s ' "$CANDIDATE"
        if timeout 12 openssl s_client -connect "$CANDIDATE:443" -servername "$CANDIDATE" \
               -tls1_3 -alpn h2 </dev/null 2>/dev/null | grep -q 'ALPN protocol: h2'; then
            echo "годится"
            SNI="$CANDIDATE"
            break
        fi
        echo "не отвечает TLS 1.3 + h2"
    done
    [ -n "$SNI" ] || die "Ни один домен не подошёл. Задайте свой: VLESS_SNI=example.com sudo -E sh deploy/vless.sh install"

    # 5. Xray
    echo
    echo "=== Xray ==="
    if ! command -v xray >/dev/null 2>&1; then
        curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh \
            | bash -s -- install >/dev/null
    fi
    command -v xray >/dev/null 2>&1 || die "Xray не установился — проверьте доступ к github.com с сервера."
    echo "$(xray version 2>/dev/null | head -1)"

    # 6. Ключи. В свежих сборках публичный ключ называется Password —
    # ловим оба написания, иначе ссылка уедет с пустым pbk.
    KEYS=$(xray x25519)
    PRIVATE=$(printf '%s\n' "$KEYS" | grep -i 'private' | head -1 | sed 's/.*[:=][[:space:]]*//')
    PUBLIC=$(printf '%s\n' "$KEYS" | grep -iE 'public|password' | head -1 | sed 's/.*[:=][[:space:]]*//')
    if [ -z "$PRIVATE" ] || [ -z "$PUBLIC" ]; then
        echo "xray x25519 ответил не так, как ожидалось:" >&2
        printf '%s\n' "$KEYS" >&2
        die "Ключи не разобрались."
    fi
    SHORT_ID=$(openssl rand -hex 8)

    # 7. Конфиг и первый клиент
    NAME="${1:-phone}"
    mkdir -p /usr/local/etc/xray
    LINK=$(py init --host "$HOST" --port "$PORT" --sni "$SNI" --dest "$SNI:443" \
        --private-key "$PRIVATE" --public-key "$PUBLIC" --short-id "$SHORT_ID" --client "$NAME")

    systemctl enable xray >/dev/null 2>&1 || true
    restart_xray

    # 8. Фаервол. iptables руками не трогаем: одна лишняя строка — и SSH
    # отваливается, а консоль Netcup спасает не мгновенно.
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw allow "$PORT"/tcp >/dev/null 2>&1 && echo "ufw: порт $PORT/tcp открыт"
    fi
    if command -v iptables >/dev/null 2>&1 && iptables -S INPUT 2>/dev/null | head -1 | grep -q DROP; then
        echo
        echo "ВНИМАНИЕ: в iptables политика INPUT — DROP. Порт нужно открыть самому:"
        echo "    iptables -I INPUT -p tcp --dport $PORT -j ACCEPT"
    fi

    echo
    echo "=== готово ==="
    echo "Клиент: $NAME"
    show_link "$LINK"
    echo "Приложения: айфон — v2RayTun или Streisand, андроид — v2rayNG или Hiddify,"
    echo "мак и винда — Hiddify. Ссылка вставляется из буфера, QR сканируется камерой."
    echo
    echo "Ещё клиент:  sudo sh deploy/vless.sh add имя"
}

# --- остальное -------------------------------------------------------

do_add() {
    need_root "add $1"
    need_installed
    [ -n "$1" ] || die "Кому? sudo sh deploy/vless.sh add vlad-iphone"
    LINK=$(py add "$1")
    restart_xray
    show_link "$LINK"
}

do_remove() {
    need_root "remove $1"
    need_installed
    [ -n "$1" ] || die "Кого? sudo sh deploy/vless.sh remove vlad-iphone"
    py remove "$1"
    restart_xray
    echo "Доступ отобран, Xray перезапущен."
}

do_link() {
    need_root "link $1"
    need_installed
    [ -n "$1" ] || die "Чью ссылку? sudo sh deploy/vless.sh link vlad-iphone"
    show_link "$(py link "$1")"
}

do_repair() {
    need_root repair
    need_installed
    py render
    restart_xray
    echo "Служба поднята. Ссылки клиентов:"
    py list
}

# Разбор «клиент показывает n/a». Каждая строка — отдельная причина,
# по которой соединение не встаёт; проверяются все, даже если первая
# уже нашлась, иначе чинить придётся по одной за круг переписки.
do_check() {
    need_root check
    need_installed
    BAD=0
    say_ok()   { printf '  ok    %s\n' "$1"; }
    say_bad()  { printf '  ПЛОХО %s\n' "$1"; BAD=$((BAD + 1)); }
    say_hmm()  { printf '  ?     %s\n' "$1"; }

    HOST=$(py get host); SNI=$(py get sni)

    echo "=== служба ==="
    if systemctl is-active --quiet xray 2>/dev/null; then
        say_ok "xray запущен"
    else
        say_bad "xray не запущен — sudo sh deploy/vless.sh repair"
        journalctl -u xray -n 5 --no-pager 2>/dev/null | sed 's/^/        /'
    fi

    XUSER=$(sed -n 's/^User=//p' /etc/systemd/system/xray.service 2>/dev/null | head -1)
    XUSER="${XUSER:-nobody}"
    if command -v runuser >/dev/null 2>&1; then
        if runuser -u "$XUSER" -- test -r "$CONFIG" 2>/dev/null; then
            say_ok "конфиг читается пользователем $XUSER"
        else
            say_bad "конфиг НЕ читается пользователем $XUSER — sudo sh deploy/vless.sh repair"
        fi
    fi

    echo
    echo "=== порт $PORT ==="
    if command -v ss >/dev/null 2>&1; then
        LINE=$(ss -lntp 2>/dev/null | grep ":$PORT " | head -1)
        if [ -n "$LINE" ]; then
            say_ok "слушается: $(echo "$LINE" | tr -s ' ')"
        else
            say_bad "никто не слушает порт $PORT"
        fi
    else
        say_hmm "нет ss — порт не проверить (apt install -y iproute2)"
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw status 2>/dev/null | grep -q "$PORT" \
            && say_ok "ufw: порт открыт" || say_bad "ufw включён, а порт $PORT не открыт"
    fi
    if command -v iptables >/dev/null 2>&1 && iptables -S INPUT 2>/dev/null | head -1 | grep -q DROP; then
        say_bad "iptables INPUT = DROP. Открыть: iptables -I INPUT -p tcp --dport $PORT -j ACCEPT"
    fi

    echo
    echo "=== ключи ==="
    # Публичный ключ в ссылке должен соответствовать приватному в конфиге.
    # Если нет — снаружи всё выглядит здоровым, а клиент молча не цепляется.
    DERIVED=$(xray x25519 -i "$(py get private_key)" 2>/dev/null \
        | grep -iE 'public|password' | head -1 | sed 's/.*[:=][[:space:]]*//')
    if [ -z "$DERIVED" ]; then
        say_hmm "эта сборка Xray не умеет проверять ключ — пропускаю"
    elif [ "$DERIVED" = "$(py get public_key)" ]; then
        say_ok "публичный ключ в ссылках соответствует приватному"
    else
        say_bad "ключи НЕ сходятся: в ссылках чужой pbk, клиент не подключится"
        echo "        починить: python3 deploy/vless_admin.py --meta $META get public_key"
        echo "        и заменить его в шпаргалке на: $DERIVED"
    fi

    echo
    echo "=== время ==="
    # TLS не прощает расхождения часов: рукопожатие не состоится.
    if command -v timedatectl >/dev/null 2>&1; then
        timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes \
            && say_ok "часы синхронизированы ($(date '+%H:%M:%S %Z'))" \
            || say_bad "часы НЕ синхронизированы — apt install -y systemd-timesyncd"
    fi

    echo
    echo "=== маскировочный домен $SNI ==="
    if timeout 12 openssl s_client -connect "$SNI:443" -servername "$SNI" \
           -tls1_3 -alpn h2 </dev/null 2>/dev/null | grep -q 'ALPN protocol: h2'; then
        say_ok "отвечает TLS 1.3 + h2"
    else
        say_bad "не отвечает — смените домен, см. docs/VLESS.md"
    fi

    echo
    echo "=== ответ Reality ==="
    # Стучимся на свой же порт так, как это делает посторонний: с SNI
    # маскировочного домена и без ключа. Здоровый Reality молча отдаёт
    # сертификат настоящего сайта. Если вместо него пусто или чужой
    # сертификат — отвечает не Xray, и клиенту делать нечего.
    SUBJECT=$(timeout 12 openssl s_client -connect "$HOST:$PORT" -servername "$SNI" \
        </dev/null 2>/dev/null | openssl x509 -noout -subject 2>/dev/null || true)
    case "$SUBJECT" in
        "")
            say_hmm "на свой адрес достучаться не вышло — у хостера так бывает,"
            printf '        проверьте с домашнего компьютера:\n'
            printf '        openssl s_client -connect %s:%s -servername %s </dev/null | head -20\n' \
                "$HOST" "$PORT" "$SNI"
            ;;
        *"$SNI"*)
            say_ok "отдаёт сертификат $SNI — маскировка работает"
            ;;
        *)
            say_bad "отвечает чужим сертификатом: $SUBJECT"
            ;;
    esac

    echo
    echo "=== адрес в ссылках ==="
    REAL=$(curl -s -m 10 https://api.ipify.org 2>/dev/null || true)
    if [ -z "$REAL" ]; then
        say_hmm "внешний адрес не определился, сверьте сами: в ссылках $HOST"
    elif [ "$REAL" = "$HOST" ]; then
        say_ok "$HOST — совпадает с реальным"
    else
        say_bad "в ссылках $HOST, а сервер отвечает с $REAL — ссылки ведут не туда"
    fi

    echo
    if [ "$BAD" = "0" ]; then
        echo "Сервер в порядке. Если клиент всё равно пишет n/a — дело в нём:"
        echo "проверьте, что при импорте подхватился flow xtls-rprx-vision и"
        echo "ссылка скопировалась целиком, до последнего символа."
        echo
        py list
    else
        echo "Проблем найдено: $BAD. Чинить сверху вниз."
    fi
}

do_status() {
    need_root status
    need_installed
    echo "служба:   $(systemctl is-active xray 2>/dev/null || echo нет)"
    py show
    if command -v ss >/dev/null 2>&1; then
        ss -lnt 2>/dev/null | grep -q ":$PORT " \
            && echo "порт $PORT: слушается" || echo "порт $PORT: НЕ слушается"
    fi
    echo "журнал:   journalctl -u xray -n 50 --no-pager"
}

do_uninstall() {
    need_root uninstall
    printf 'Снести Xray и всех клиентов? Бот не пострадает. [y/N] '
    read -r ANSWER
    case "$ANSWER" in
        y|Y|yes|да) ;;
        *) echo "Отменено."; exit 0 ;;
    esac
    curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh | bash -s -- remove --purge || true
    rm -f "$META" "$CONFIG"
    echo "Снесено."
}

COMMAND="${1:-}"
[ $# -gt 0 ] && shift || true
case "$COMMAND" in
    install)   do_install "$@" ;;
    add)       do_add "${1:-}" ;;
    remove)    do_remove "${1:-}" ;;
    link)      do_link "${1:-}" ;;
    list)      need_root list; need_installed; py list ;;
    status)    do_status ;;
    repair)    do_repair ;;
    check)     do_check ;;
    uninstall) do_uninstall ;;
    *)         awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0" ;;
esac
