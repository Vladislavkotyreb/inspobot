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
#   sudo sh deploy/vless.sh selftest       пройти через туннель самому
#   sudo sh deploy/vless.sh diagnose       перебрать варианты, если туннель не встал
#   sudo sh deploy/vless.sh set-domain X   сменить маскировочный домен
#   sudo sh deploy/vless.sh set-fingerprint X  сменить отпечаток ClientHello
#   sudo sh deploy/vless.sh fp-links       ссылки с разными отпечатками для перебора
#   sudo sh deploy/vless.sh probe-links    ссылки на несколько доменов сразу
#   sudo sh deploy/vless.sh probe-clear    убрать пробные входы
#   sudo sh deploy/vless.sh reach          доходят ли до сервера из России
#   sudo sh deploy/vless.sh watch          смотреть, что приходит на сервер
#   sudo sh deploy/vless.sh set-host АДРЕС новый адрес сервера в ссылках
#   sudo sh deploy/vless.sh transport xhttp|tcp  транспорт основного входа
#   sudo sh deploy/vless.sh ss [ПОРТ]      вход Shadowsocks (без рукопожатия TLS)
#   sudo sh deploy/vless.sh ss-clear       убрать вход Shadowsocks
#   sudo sh deploy/vless.sh cdn ДОМЕН      маршрут через Cloudflare
#   sudo sh deploy/vless.sh cdn-check      проверить, что Cloudflare достаёт до сервера
#   sudo sh deploy/vless.sh cdn-clear      убрать маршрут через CDN
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
# Куда ходим, чтобы узнать свой адрес. Меняется на случай, когда
# этот сервис недоступен из сети сервера.
CHECK_URL="${VLESS_CHECK_URL:-https://api.ipify.org}"

# Маскировочные домены: Reality притворяется трафиком к одному из них.
# Мало отвечать TLS 1.3 с HTTP/2 — домен обязан ещё и выдержать
# настоящее рукопожатие REALITY, а это проверяется только попыткой.
# www.microsoft.com из списка убран: поверхностную проверку он проходит,
# а рукопожатие с ним не собирается (проверено на немецком узле Netcup).
# Свой вариант: VLESS_SNI=example.com sh deploy/vless.sh install
SNI_CANDIDATES="${VLESS_SNI:-dl.google.com www.samsung.com www.apple.com www.asus.com www.nvidia.com}"

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

# Годится ли домен под маскировку — проверяется единственным честным
# способом: поднять рядом такой же сервер с этим доменом и пройти через
# него. Проверка «отвечает ли он TLS 1.3» ничего не гарантирует: именно
# так в конфиг попал домен, с которым рукопожатие не собиралось.
probe_domain() {
    TRY="$1"
    PPORT=8443
    while ss -lnt 2>/dev/null | grep -q ":$PPORT "; do PPORT=$((PPORT + 1)); done
    PSOCKS=10808
    while ss -lnt 2>/dev/null | grep -q "127.0.0.1:$PSOCKS "; do PSOCKS=$((PSOCKS + 1)); done

    PDIR=$(mktemp -d)
    if ! python3 "$DIR/vless_probe.py" --meta "$META" --dir "$PDIR" --variant как-есть \
            --sni "$TRY" --port "$PPORT" --socks "$PSOCKS" >/dev/null 2>&1; then
        rm -rf "$PDIR"
        return 1
    fi
    xray run -c "$PDIR/server.json" > "$PDIR/server.log" 2>&1 &
    PSRV=$!
    xray run -c "$PDIR/client.json" > "$PDIR/client.log" 2>&1 &
    PCLI=$!
    sleep 2
    POUT=""
    if kill -0 "$PSRV" 2>/dev/null && kill -0 "$PCLI" 2>/dev/null; then
        POUT=$(curl -s -m 12 --socks5-hostname "127.0.0.1:$PSOCKS" "$CHECK_URL" 2>/dev/null || true)
    fi
    kill "$PSRV" "$PCLI" 2>/dev/null
    wait "$PSRV" 2>/dev/null || true
    wait "$PCLI" 2>/dev/null || true
    rm -rf "$PDIR"
    [ -n "$POUT" ]
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
        HOST=$(curl -s -m 10 "$CHECK_URL" || true)
    fi
    [ -n "$HOST" ] || die "Не определил внешний адрес. Задайте руками: VLESS_HOST=1.2.3.4 sudo -E sh deploy/vless.sh install"
    echo "Адрес сервера: $HOST"

    # 4. Xray
    echo
    echo "=== Xray ==="
    if ! command -v xray >/dev/null 2>&1; then
        curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh \
            | bash -s -- install >/dev/null
    fi
    command -v xray >/dev/null 2>&1 || die "Xray не установился — проверьте доступ к github.com с сервера."
    echo "$(xray version 2>/dev/null | head -1)"

    # 5. Ключи. В свежих сборках публичный ключ называется Password —
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

    # 6. Конфиг и первый клиент. Домен пока любой из списка: настоящий
    #    подберём следующим шагом, для него уже нужны ключи и клиент.
    NAME="${1:-phone}"
    FIRST=$(printf '%s\n' $SNI_CANDIDATES | head -1)
    mkdir -p /usr/local/etc/xray
    py init --host "$HOST" --port "$PORT" --sni "$FIRST" --dest "$FIRST:443" \
        --private-key "$PRIVATE" --public-key "$PUBLIC" --short-id "$SHORT_ID" \
        --client "$NAME" >/dev/null

    # 7. Маскировочный домен — перебором с настоящим рукопожатием.
    #    Проверка «отвечает ли домен TLS 1.3» недостаточна: домен может
    #    её пройти и всё равно не дать собрать рукопожатие REALITY.
    echo
    echo "=== маскировка ==="
    SNI=""
    for CANDIDATE in $SNI_CANDIDATES; do
        printf '  %s ... ' "$CANDIDATE"
        if probe_domain "$CANDIDATE"; then
            echo "годится"
            SNI="$CANDIDATE"
            break
        fi
        echo "рукопожатие не собирается"
    done
    if [ -z "$SNI" ]; then
        rm -f "$META" "$CONFIG"
        die "Ни один домен не подошёл. Задайте свой: VLESS_SNI=example.com sudo -E sh deploy/vless.sh install"
    fi
    py set-domain "$SNI" >/dev/null
    LINK=$(py link "$NAME")

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
    echo "Маскировка: $SNI"
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
    PROBE=$(timeout 12 openssl s_client -connect "$SNI:443" -servername "$SNI" \
        -tls1_3 -alpn h2 </dev/null 2>/dev/null || true)
    if printf '%s\n' "$PROBE" | grep -q 'ALPN protocol: h2'; then
        say_ok "отвечает TLS 1.3 + h2"
    else
        say_bad "не отвечает — смените домен, см. docs/VLESS.md"
    fi
    # REALITY прячет аутентификацию внутри обмена ключами X25519. Если
    # домен согласует пост-квантовый гибрид (X25519MLKEM768), прятать
    # становится некуда, и рукопожатие отвергается — при полностью
    # исправных ключах и настройках.
    # Размер ответа — та самая причина, по которой домен может быть
    # безупречно доступен и при этом непригоден.
    python3 "$DIR/vless_domains.py" "$SNI" 2>/dev/null | head -1 | sed 's/^/ /' || true
    GROUP=$(printf '%s\n' "$PROBE" | grep -i 'Negotiated TLS1.3 group' | head -1 | sed 's/.*: //')
    case "$GROUP" in
        "")        say_hmm "группа обмена ключами не показана (старый openssl)" ;;
        *MLKEM*|*mlkem*|*Kyber*)
            say_bad "домен согласует $GROUP — пост-квантовый обмен, REALITY с ним не работает"
            echo "        смените домен: sudo sh deploy/vless.sh diagnose"
            ;;
        *)         say_ok "обмен ключами: $GROUP" ;;
    esac

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
    REAL=$(curl -s -m 10 "$CHECK_URL" 2>/dev/null || true)
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

# Проход через туннель с самого сервера — двумя путями.
#
# Через 127.0.0.1 проверяется рукопожатие само по себе: Reality
# смотрит на SNI, а не на адрес, поэтому конфиг проверяется полностью,
# минуя сеть хостера. Через внешний адрес — то же самое, но так, как
# это делает телефон. Второе у многих хостеров не проходит никогда:
# сервер не умеет ходить на свой же внешний адрес. Поэтому важен
# именно первый путь, а второй — справочно.
do_selftest() {
    need_root selftest
    need_installed
    command -v xray >/dev/null 2>&1 || die "Нет xray."

    NAME="${1:-}"
    [ -n "$NAME" ] || NAME=$(py names | head -1)
    [ -n "$NAME" ] || die "Кого проверяем? sudo sh deploy/vless.sh selftest phone"

    WORK=$(mktemp -d)
    CLIENT_PID=""
    RESTORE=0
    cleanup() {
        [ -n "$CLIENT_PID" ] && kill "$CLIENT_PID" 2>/dev/null
        # Подробный журнал — только на время разбора: он пишет в syslog
        # каждое соединение, а это и место, и приватность.
        if [ "$RESTORE" = "1" ]; then
            py render >/dev/null 2>&1 && systemctl restart xray 2>/dev/null
            echo "Журнал сервера возвращён в обычный режим."
        fi
        rm -rf "$WORK"
    }
    trap cleanup EXIT INT TERM

    # Печатает адрес, с которого вышли наружу, или пусто.
    attempt() {
        SOCKS=10808
        if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | grep -q "127.0.0.1:$SOCKS "; then
            SOCKS=10809
        fi
        py client-config "$NAME" --socks-port "$SOCKS" --address "$1" > "$WORK/client.json"
        xray run -c "$WORK/client.json" > "$WORK/log" 2>&1 &
        CLIENT_PID=$!
        sleep 3
        OUT=""
        if kill -0 "$CLIENT_PID" 2>/dev/null; then
            OUT=$(curl -s -m 20 --socks5-hostname "127.0.0.1:$SOCKS" "$CHECK_URL" 2>/dev/null || true)
        fi
        kill "$CLIENT_PID" 2>/dev/null
        wait "$CLIENT_PID" 2>/dev/null || true
        CLIENT_PID=""
        printf '%s' "$OUT"
    }

    HOSTADDR=$(py get host)

    echo "=== рукопожатие (через 127.0.0.1) ==="
    LOCAL=$(attempt 127.0.0.1)
    if [ -n "$LOCAL" ]; then
        echo "  ok    прошло, вышли с адреса $LOCAL"
    else
        echo "  ПЛОХО туннель не встал. Поднимаю подробный журнал и повторяю."
        SINCE=$(date '+%Y-%m-%d %H:%M:%S')
        py render --loglevel debug >/dev/null
        systemctl restart xray
        RESTORE=1
        sleep 1
        attempt 127.0.0.1 >/dev/null
        echo
        echo "--- журнал сервера ---"
        journalctl -u xray --since "$SINCE" --no-pager 2>/dev/null | tail -40 | sed 's/^/    /'
        echo "--- журнал клиента ---"
        sed 's/^/    /' "$WORK/log"
        echo
        echo "Конфигурация не работает даже внутри машины — дело не в приложении."
        exit 1
    fi

    echo
    echo "=== тот же путь, но через внешний адрес $HOSTADDR ==="
    EXTERNAL=$(attempt "$HOSTADDR")
    if [ -n "$EXTERNAL" ]; then
        echo "  ok    прошло, вышли с адреса $EXTERNAL"
    else
        echo "  ?     не прошло — у многих хостеров сервер не умеет ходить"
        echo "        на свой же внешний адрес. Само по себе это не поломка:"
        echo "        рукопожатие выше прошло, значит конфигурация рабочая."
    fi

    echo
    echo "Конфигурация сервера исправна. Ссылка для клиента:"
    py link "$NAME"
}

# Перебор вариантов. Поднимаем рядом, на запасном порту, такой же
# сервер — меняя по одной вещи за раз — и смотрим, где рукопожатие
# пройдёт. Живую службу на 443 не трогаем вовсе.
do_diagnose() {
    need_root diagnose
    need_installed
    command -v xray >/dev/null 2>&1 || die "Нет xray."

    NAME="${1:-}"
    [ -n "$NAME" ] || NAME=$(py names | head -1)

    PROBE_PORT=8443
    PROBE_SOCKS=10808
    while ss -lnt 2>/dev/null | grep -q ":$PROBE_PORT "; do
        PROBE_PORT=$((PROBE_PORT + 1))
    done
    while ss -lnt 2>/dev/null | grep -q "127.0.0.1:$PROBE_SOCKS "; do
        PROBE_SOCKS=$((PROBE_SOCKS + 1))
    done

    WORK=$(mktemp -d)
    SRV_PID=""
    CLI_PID=""
    cleanup() {
        [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
        [ -n "$CLI_PID" ] && kill "$CLI_PID" 2>/dev/null
        rm -rf "$WORK"
    }
    trap cleanup EXIT INT TERM

    echo "Пробный сервер на порту $PROBE_PORT, живая служба на $PORT не тронута."
    echo "Каждый вариант отличается от рабочего ровно одной вещью."
    echo

    WORKED=""
    for VARIANT in $(python3 "$DIR/vless_probe.py" --list); do
        printf '  %s ... ' "$VARIANT"
        rm -rf "$WORK/v"
        if ! python3 "$DIR/vless_probe.py" --meta "$META" --dir "$WORK/v" \
                --variant "$VARIANT" --client "$NAME" \
                --port "$PROBE_PORT" --socks "$PROBE_SOCKS" 2>"$WORK/err"; then
            echo "конфиг не собрался: $(cat "$WORK/err")"
            continue
        fi

        xray run -c "$WORK/v/server.json" > "$WORK/v/server.log" 2>&1 &
        SRV_PID=$!
        xray run -c "$WORK/v/client.json" > "$WORK/v/client.log" 2>&1 &
        CLI_PID=$!
        sleep 2

        OUT=""
        if kill -0 "$SRV_PID" 2>/dev/null && kill -0 "$CLI_PID" 2>/dev/null; then
            OUT=$(curl -s -m 10 --socks5-hostname "127.0.0.1:$PROBE_SOCKS" \
                "$CHECK_URL" 2>/dev/null || true)
        fi

        if [ -n "$OUT" ]; then
            echo "РАБОТАЕТ"
            [ -z "$WORKED" ] && WORKED="$VARIANT"
        else
            REASON=$(grep -o 'REALITY: [^"]*' "$WORK/v/server.log" 2>/dev/null | tail -1)
            [ -z "$REASON" ] && REASON=$(tail -1 "$WORK/v/server.log" 2>/dev/null)
            echo "нет${REASON:+  ($REASON)}"
        fi

        kill "$SRV_PID" "$CLI_PID" 2>/dev/null
        wait "$SRV_PID" 2>/dev/null || true
        wait "$CLI_PID" 2>/dev/null || true
        SRV_PID=""
        CLI_PID=""
    done

    echo
    if [ "$WORKED" = "как-есть" ]; then
        echo "Та же конфигурация на запасном порту $PROBE_PORT работает."
        echo "Значит, дело не в настройках, а в живой службе или в порте $PORT."
        echo
        echo "Что сейчас в живом конфиге (приватный ключ скрыт):"
        python3 - "$CONFIG" <<'INNER' | sed 's/^/    /'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
inbound = data["inbounds"][0]
reality = dict(inbound["streamSettings"]["realitySettings"])
reality["privateKey"] = "<скрыт>"
print("listen:", inbound.get("listen"), "port:", inbound.get("port"))
print("clients:", [(c.get("email"), c.get("flow")) for c in inbound["settings"]["clients"]])
print("reality:", json.dumps(reality, ensure_ascii=False))
INNER
    elif [ -n "$WORKED" ]; then
        echo "Заработало на варианте: $WORKED"
        echo "Пришлите эту строку — переведу рабочую конфигурацию на него."
    else
        echo "Не заработал ни один вариант. Значит, дело не в этих настройках."
        echo "Пришлите вывод целиком."
    fi
}

do_set_domain() {
    need_root "set-domain $1"
    need_installed
    [ -n "$1" ] || die "Какой домен? sudo sh deploy/vless.sh set-domain dl.google.com"
    command -v xray >/dev/null 2>&1 || die "Нет xray."

    printf 'Проверяю %s настоящим рукопожатием... ' "$1"
    if probe_domain "$1"; then
        echo "годится"
    else
        echo "НЕ ГОДИТСЯ"
        die "Через него рукопожатие не проходит — домен не меняю. Подобрать: sudo sh deploy/vless.sh diagnose"
    fi

    py set-domain "$1" >/dev/null
    restart_xray
    echo
    echo "Домен сменён на $1, служба перезапущена."
    echo
    echo "ВАЖНО: домен зашит в каждую ссылку, поэтому все прежние ссылки"
    echo "перестали работать. Раздайте новые — вот они:"
    echo
    py list
}

# Несколько пробных входов на запасных портах, каждый со своим
# маскировочным доменом. Человеку на той стороне отправляются все
# ссылки разом: какая подключится, тот домен и проходит через его
# провайдера. Иначе на каждый домен уходит круг переписки.
# Отпечаток — свойство ссылки, серверу он безразличен: перезапуск не
# нужен, но все прежние ссылки после смены устаревают.
do_set_fingerprint() {
    need_root "set-fingerprint $1"
    need_installed
    [ -n "$1" ] || die "Какой? sudo sh deploy/vless.sh set-fingerprint safari"
    py set-fingerprint "$1" >/dev/null
    echo "Отпечаток: $1. Служба не перезапускалась — ей это не нужно."
    echo
    echo "Ссылки изменились, раздайте новые:"
    echo
    py list
}

# Набор ссылок для человека на той стороне: та же связка, меняется
# только отпечаток ClientHello. Ему нужен только Happ — импортирует все,
# пробует по очереди, говорит, какая ожила.
do_fp_links() {
    need_root fp-links
    need_installed
    NAME="${1:-}"
    [ -n "$NAME" ] || NAME=$(py names | head -1)
    echo "Одна связка ($(py get sni), порт $(py get port)), пять отпечатков."
    echo "Отправьте все пять. В Happ метка каждой заканчивается на имя отпечатка."
    echo
    py fp-links "$NAME"
}

do_probe_links() {
    need_root probe-links
    need_installed
    command -v xray >/dev/null 2>&1 || die "Нет xray."

    NAME="${1:-}"
    [ -n "$NAME" ] || NAME=$(py names | head -1)

    DOMAINS="${VLESS_PROBE_DOMAINS:-www.bing.com www.samsung.com addons.mozilla.org www.apple.com}"
    LIVE=$(py get sni)

    echo "=== замер доменов ==="
    echo "Ответ рукопожатия должен уложиться в 8192 Б — предел буфера в Xray 26.x."
    python3 "$DIR/vless_domains.py" $DOMAINS || true

    echo
    echo "=== проверка рукопожатием ==="
    PAIRS=""
    NEXT=8443
    for CANDIDATE in $DOMAINS; do
        [ "$CANDIDATE" = "$LIVE" ] && continue
        while ss -lnt 2>/dev/null | grep -q ":$NEXT "; do NEXT=$((NEXT + 1)); done
        printf '  %s ... ' "$CANDIDATE"
        if probe_domain "$CANDIDATE"; then
            echo "годится, порт $NEXT"
            PAIRS="$PAIRS $CANDIDATE:$NEXT"
            NEXT=$((NEXT + 1))
        else
            echo "рукопожатие не собирается — пропускаю"
        fi
    done

    # Плюс рабочий домен без Vision: это отдельный слой поверх REALITY
    # со своими условиями, и снаружи его иначе не проверить.
    while ss -lnt 2>/dev/null | grep -q ":$NEXT "; do NEXT=$((NEXT + 1)); done
    PAIRS="$PAIRS $LIVE:$NEXT:novision"
    echo "  $LIVE без Vision ... порт $NEXT"

    py set-alts $PAIRS >/dev/null
    restart_xray

    echo
    echo "=== отправьте эти ссылки тому, у кого не подключается ==="
    echo "Пусть импортирует ВСЕ и попробует по очереди. Которая заработает —"
    echo "пришлите её название, переведу основной вход на этот домен."
    echo
    echo "рабочий сейчас: $LIVE (порт $PORT)"
    py link "$NAME"
    echo
    py alt-links "$NAME"
    echo "Убрать пробные входы потом: sudo sh deploy/vless.sh probe-clear"
}

do_probe_clear() {
    need_root probe-clear
    need_installed
    py clear-alts
    restart_xray
    echo "Пробные входы убраны, остался основной."
}

# Доходят ли до сервера из России. Отвечает на вопрос, который с
# сервера не решается никак: заблокирован ли сам адрес. Если да —
# смена маскировочного домена бесполезна, и надо менять адрес.
do_reach() {
    need_root reach
    need_installed
    HOSTADDR=$(py get host)
    PORTS="${1:-$(py get port)}"
    for TRY in $PORTS; do
        echo "=== порт $TRY ==="
        python3 "$DIR/vless_reach.py" --host "$HOSTADDR" --port "$TRY" || true
        echo
    done
}

# Смотреть, что приходит на сервер прямо сейчас. Это разделяет две
# причины, которые снаружи выглядят одинаково: «до сервера не доходит»
# и «доходит, но сервер отвергает». Пока этого не видно, любой разбор —
# гадание.
do_watch() {
    need_root watch
    need_installed
    SECONDS_TO_WATCH="${1:-180}"

    restore() {
        py render >/dev/null 2>&1 && systemctl restart xray 2>/dev/null
        echo
        echo "Журнал сервера возвращён в обычный режим."
    }
    trap restore EXIT INT TERM

    py render --loglevel info >/dev/null
    systemctl restart xray
    sleep 1

    echo "=== слушаю $SECONDS_TO_WATCH секунд ==="
    echo "Сейчас запустите проверку с другого компьютера. Что увижу:"
    echo
    echo "  строки REALITY  — ваш ClientHello ДОШЁЛ, сервер его отверг;"
    echo "  «received request» — туннель поднялся;"
    echo "  тишина          — до сервера не дошло ничего."
    echo
    timeout "$SECONDS_TO_WATCH" journalctl -u xray -f -n 0 --no-pager 2>/dev/null \
        | grep --line-buffered -E 'REALITY|received request|rejected|failed' \
        | sed 's/^/  /' || true
    echo
    echo "=== время вышло ==="
}

# Файл должен читаться демоном: тот работает от nobody, и закрытый
# от root ключ он не откроет — как это уже было с конфигом.
harden_for_xray() {
    XUSER=$(sed -n 's/^User=//p' /etc/systemd/system/xray.service 2>/dev/null | head -1)
    XUSER="${XUSER:-nobody}"
    XGROUP=$(id -gn "$XUSER" 2>/dev/null || echo nogroup)
    chown "root:$XGROUP" "$1" 2>/dev/null || true
    chmod 0640 "$1"
}

# Маршрут через CDN. Когда DPI режет любой TLS к нашему адресу, до него
# можно добраться только не ходя на него: человек идёт на адрес
# Cloudflare — для DPI это обычный сайт, — а Cloudflare ходит сюда.
# Транспорт XHTTP: он задуман для CDN, а WebSocket в Xray 26 объявлен
# устаревшим. Сертификат самоподписанный: Cloudflare в режиме Full
# принимает любой, а человеку показывает свой, настоящий.
do_cdn() {
    need_root "cdn $1"
    need_installed
    DOMAIN="${1:-}"
    [ -n "$DOMAIN" ] || die "Какой домен? sudo sh deploy/vless.sh cdn example.com"
    case "$DOMAIN" in
        *[!A-Za-z0-9.-]*) die "Домен $DOMAIN выглядит неправильно: только латиница, цифры, точки, дефис." ;;
    esac

    CRT=/usr/local/etc/xray/cdn.crt
    KEY=/usr/local/etc/xray/cdn.key
    if [ ! -f "$KEY" ]; then
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
            -keyout "$KEY" -out "$CRT" -days 3650 -subj "/CN=$DOMAIN" 2>/dev/null \
            || die "Не удалось выпустить сертификат."
    fi
    harden_for_xray "$KEY"
    harden_for_xray "$CRT"

    WSPATH="/$(openssl rand -hex 6)"
    OLD_PORT=$(py get port)
    py cdn-setup --domain "$DOMAIN" --cert "$CRT" --key "$KEY" --path "$WSPATH" >/dev/null
    restart_xray

    NEW_PORT=$(py get port)
    echo "=== маршрут через CDN поднят ==="
    echo "Домен: $DOMAIN, вход на 443, путь $WSPATH"
    if [ "$OLD_PORT" != "$NEW_PORT" ]; then
        echo "REALITY переехал с $OLD_PORT на $NEW_PORT: 443 теперь у CDN."
        echo "Прежние REALITY-ссылки устарели — новые ниже."
    fi
    echo
    echo "Проверить, что Cloudflare настроен и достаёт до сервера:"
    echo "    sudo sh deploy/vless.sh cdn-check"
    echo
    echo "Ссылки через CDN — их и раздавать людям в России:"
    echo
    py cdn-links
    echo "Ссылки REALITY (напрямую, для тех, кого не режут):"
    echo
    py list
}

# Проверка снаружи внутрь: отвечает ли за домен Cloudflare, и достаёт
# ли он до нашего Xray. Идёт с самого сервера через интернет, как
# пошёл бы человек.
do_cdn_check() {
    need_root cdn-check
    need_installed
    DOMAIN=$(py get cdn.domain 2>/dev/null || true)
    WSPATH=$(py get cdn.path 2>/dev/null || true)
    [ -n "$DOMAIN" ] || die "Маршрут через CDN не настроен: sudo sh deploy/vless.sh cdn домен"
    BAD=0

    echo "=== $DOMAIN ==="
    RESOLVED=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | head -3 | tr '\n' ' ')
    if [ -z "$RESOLVED" ]; then
        echo "  ПЛОХО домен не резолвится — nameserver'ы ещё не переключились на Cloudflare"
        BAD=$((BAD + 1))
    elif echo "$RESOLVED" | grep -q "$(py get host)"; then
        echo "  ПЛОХО домен указывает прямо на наш адрес ($RESOLVED) — облако в DNS серое, нужно оранжевое (Proxied)"
        BAD=$((BAD + 1))
    else
        echo "  ok    домен резолвится в $RESOLVED (не в наш адрес — значит, через Cloudflare)"
    fi

    HEADERS=$(curl -sI -m 20 "https://$DOMAIN/" 2>/dev/null | tr -d '\r')
    if printf '%s\n' "$HEADERS" | grep -qi '^server: *cloudflare'; then
        echo "  ok    за домен отвечает Cloudflare"
    else
        echo "  ПЛОХО Cloudflare не отвечает за домен (нет заголовка server: cloudflare)"
        BAD=$((BAD + 1))
    fi

    CODE=$(curl -s -o /dev/null -m 20 -w '%{http_code}' "https://$DOMAIN$WSPATH" 2>/dev/null || echo 000)
    case "$CODE" in
        400|404|405|426)
            echo "  ok    Cloudflare достучался до Xray (ответ $CODE на обычный запрос — так и должно быть)" ;;
        52[0-9])
            echo "  ПЛОХО Cloudflare не достучался до сервера (ошибка $CODE)."
            echo "        Проверьте режим SSL/TLS = Full и что порт 443 у нас слушает Xray."
            BAD=$((BAD + 1)) ;;
        000)
            echo "  ПЛОХО домен не отвечает вовсе"
            BAD=$((BAD + 1)) ;;
        *)
            echo "  ?     ответ $CODE — неожиданно, но не обязательно плохо" ;;
    esac

    echo
    if [ "$BAD" = "0" ]; then
        echo "Маршрут через CDN исправен. Проверить со своего компьютера настоящим клиентом:"
        echo "    sh deploy/test-link.sh '<ссылка через CDN>'"
    else
        echo "Проблем: $BAD. После починки в Cloudflare подождите пару минут и повторите."
    fi
}

do_cdn_clear() {
    need_root cdn-clear
    need_installed
    py cdn-clear
    restart_xray
    echo "Маршрут через CDN убран. REALITY остался на порту $(py get port)."
}

# Вход Shadowsocks-2022. Ставится там, где DPI убивает рукопожатие
# TLS: у Shadowsocks его нет вовсе — на проводе поток случайных на вид
# байт с первого байта, опознавать нечего. По умолчанию на 443, потому
# что нестандартные порты у операторов закрывают чаще, а на 443 TCP
# обычно проходит; REALITY при этом уступает порт и уезжает.
do_ss() {
    need_root ss
    need_installed
    SSPORT="${1:-443}"
    case "$SSPORT" in
        ''|*[!0-9]*) die "Порт числом: sudo sh deploy/vless.sh ss 443" ;;
    esac

    # Ключ ровно нужной длины: 2022-blake3-aes-128-gcm требует 16 байт.
    PASSWORD=$(openssl rand -base64 16)
    OLD_PORT=$(py get port)
    py ss-setup --port "$SSPORT" --password "$PASSWORD" --name "$(hostname -s 2>/dev/null || echo vpn)" >/dev/null
    restart_xray
    NEW_PORT=$(py get port)

    echo "=== вход Shadowsocks поднят на порту $SSPORT ==="
    if [ "$OLD_PORT" != "$NEW_PORT" ]; then
        echo "REALITY уступил $OLD_PORT и переехал на $NEW_PORT — его ссылки изменились."
    fi
    echo
    echo "Ссылка — её и отправлять туда, где REALITY не проходит:"
    echo
    py ss-link
    echo
    echo "В Happ: «+» → из буфера обмена. Протокол Shadowsocks, не VLESS."
    echo "Проверить со своего компьютера: sh deploy/test-link.sh '<ссылка>'"
}

do_ss_clear() {
    need_root ss-clear
    need_installed
    py ss-clear
    restart_xray
    echo "Вход Shadowsocks убран."
}

# Транспорт основного входа. Голый TCP с Vision фильтры узнают по
# рисунку трафика; xhttp заворачивает поток в обычные HTTP-запросы, и
# рисунок получается браузерный. Vision с xhttp несовместим и снимается.
# Смена адреса в ссылках. Xray слушает 0.0.0.0 и принимает все адреса
# машины, поэтому добавленный у хостера второй IPv4 начинает работать
# сразу — надо лишь выпустить ссылки на него. Прежний адрес при этом
# продолжает работать для тех, у кого он не заблокирован.
do_set_host() {
    need_root "set-host $1"
    need_installed
    NEWHOST="${1:-}"
    [ -n "$NEWHOST" ] || die "Какой адрес? sudo sh deploy/vless.sh set-host 1.2.3.4"
    case "$NEWHOST" in
        *[!0-9a-fA-F.:]*) die "Адрес $NEWHOST выглядит неправильно." ;;
    esac
    if ! ip -o addr show 2>/dev/null | grep -qw "$NEWHOST"; then
        echo "ВНИМАНИЕ: адрес $NEWHOST не найден среди адресов этой машины." >&2
        echo "Адреса, которые у неё есть:" >&2
        ip -o -4 addr show 2>/dev/null | awk '{print "    " $4}' >&2
        die "Сначала добавьте адрес у хостера и в систему, потом повторите."
    fi
    OLD=$(py get host)
    py set-host "$NEWHOST" >/dev/null
    echo "Адрес в ссылках: $OLD → $NEWHOST"
    echo "Служба не перезапускалась: Xray слушает все адреса машины."
    echo
    echo "Новые ссылки:"
    echo
    py list
}

do_transport() {
    need_root "transport $1"
    need_installed
    NETWORK="${1:-}"
    case "$NETWORK" in
        tcp|xhttp) ;;
        *) die "Транспорт: tcp или xhttp. Например: sudo sh deploy/vless.sh transport xhttp" ;;
    esac

    if [ "$NETWORK" = "xhttp" ]; then
        WSPATH="$(py get path 2>/dev/null || true)"
        case "$WSPATH" in
            /?*) ;;
            *) WSPATH="/$(openssl rand -hex 6)" ;;
        esac
        py set-transport xhttp --path "$WSPATH" >/dev/null
    else
        py set-transport tcp >/dev/null
    fi
    restart_xray

    echo "Транспорт основного входа: $NETWORK"
    [ "$NETWORK" = "xhttp" ] && echo "Vision снят — с xhttp он несовместим."
    echo
    echo "Ссылки изменились, раздайте новые:"
    echo
    py list
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
    selftest)  do_selftest "${1:-}" ;;
    diagnose)  do_diagnose "${1:-}" ;;
    set-domain) do_set_domain "${1:-}" ;;
    set-fingerprint) do_set_fingerprint "${1:-}" ;;
    fp-links)  do_fp_links "${1:-}" ;;
    probe-links) do_probe_links "${1:-}" ;;
    probe-clear) do_probe_clear ;;
    reach)     do_reach "${1:-}" ;;
    watch)     do_watch "${1:-}" ;;
    set-host)  do_set_host "${1:-}" ;;
    transport) do_transport "${1:-}" ;;
    ss)        do_ss "${1:-}" ;;
    ss-clear)  do_ss_clear ;;
    ss-link)   need_root ss-link; need_installed; py ss-link ;;
    cdn)       do_cdn "${1:-}" ;;
    cdn-check) do_cdn_check ;;
    cdn-clear) do_cdn_clear ;;
    cdn-links) need_root cdn-links; need_installed; py cdn-links ;;
    uninstall) do_uninstall ;;
    *)         awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0" ;;
esac
