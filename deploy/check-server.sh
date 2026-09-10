#!/usr/bin/env sh
# Разведка перед установкой: годится ли сервер.
# Скопируйте файл на сервер (или запустите строкой из docs/DEPLOY.md) и
# пришлите вывод целиком — по нему видно, что настраивать.
#
#   sh check-server.sh

echo "=== система ==="
uname -s -r -m
if [ -f /etc/os-release ]; then
    . /etc/os-release 2>/dev/null
    echo "${PRETTY_NAME:-неизвестно}"
fi
echo "пользователь: $(id -un), домашний каталог: $HOME"

echo
echo "=== python ==="
FOUND=""
for CMD in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$CMD" >/dev/null 2>&1; then
        printf '%-12s %s\n' "$CMD" "$("$CMD" --version 2>&1)"
        [ -z "$FOUND" ] && FOUND="$CMD"
    fi
done
[ -z "$FOUND" ] && echo "python3 не найден"

if [ -n "$FOUND" ]; then
    if "$FOUND" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        echo "версия подходит (нужен 3.11+)"
    else
        echo "ВЕРСИЯ СТАРАЯ: нужен 3.11+, иначе не поставится anthropic"
    fi
    "$FOUND" -c 'import venv' 2>/dev/null && echo "venv: есть" || echo "venv: НЕТ (нужен пакет python3-venv)"
fi

echo
echo "=== сеть ==="
# Любой трёхзначный код означает, что до хоста дошли. 000 или пусто — нет.
check_host() {
    CODE=$(curl -sS -o /dev/null -m 20 -w '%{http_code}' "$1" 2>/dev/null)
    case "$CODE" in
        000|"") printf '%-22s НЕДОСТУПЕН\n' "$2" ;;
        *)      printf '%-22s доступен (%s)\n' "$2" "$CODE" ;;
    esac
}
if command -v curl >/dev/null 2>&1; then
    check_host "https://api.anthropic.com/v1/models" "api.anthropic.com"
    check_host "https://api.mobbin.com/mcp" "api.mobbin.com"
    check_host "https://api.telegram.org/bot0:0/getMe" "api.telegram.org"
    check_host "https://github.com" "github.com"
else
    echo "curl не установлен — поставьте его, он нужен и для проверки, и для установки"
fi

echo
echo "=== расписание ==="
command -v crontab >/dev/null 2>&1 && echo "crontab: есть" || echo "crontab: НЕТ"
command -v systemctl >/dev/null 2>&1 && echo "systemd: есть" || echo "systemd: нет"
command -v git >/dev/null 2>&1 && echo "git: есть" || echo "git: НЕТ"
echo "время сервера: $(date '+%Y-%m-%d %H:%M:%S %Z')"

echo
echo "=== место ==="
df -h "$HOME" 2>/dev/null | tail -1
