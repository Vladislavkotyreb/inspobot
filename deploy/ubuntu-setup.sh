#!/usr/bin/env sh
# Установка бота на чистую Ubuntu — одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/Vladislavkotyreb/inspobot/claude/inspobot-mobbin-msp-v2p3u1/deploy/ubuntu-setup.sh | sh
#
# Или, если файл уже рядом:
#
#   sh deploy/ubuntu-setup.sh [каталог]
#
# Делает ровно четыре вещи: ставит системные пакеты, приводит часовой пояс,
# забирает код и передаёт управление install-server.sh. Повторный запуск
# безопасен: пакеты уже стоят, репозиторий обновляется, .env не трогается.
#
# Ничего не спрашивает — скрипт рассчитан на запуск через конвейер, где
# ответить на вопрос нечем.

set -e

REPO="${INSPOBOT_REPO:-https://github.com/Vladislavkotyreb/inspobot}"
BRANCH="${INSPOBOT_BRANCH:-claude/inspobot-mobbin-msp-v2p3u1}"
WANT_TZ="${INSPOBOT_TZ:-Europe/Moscow}"

# Первый аргумент — каталог, но только если это не флаг: остальные аргументы
# уходят дальше, в install-server.sh. Без разбора «ubuntu-setup.sh --cron»
# попыталось бы склонировать репозиторий в каталог с именем «--cron».
DIR_ARG=""
case "${1:-}" in
    "" | --*) : ;;
    *) DIR_ARG="$1"; shift ;;
esac
DIR="${DIR_ARG:-${INSPOBOT_DIR:-/opt/inspobot}}"

say() { printf '\n=== %s ===\n' "$1"; }

# Под обычным пользователем apt и timedatectl требуют sudo; под root он не
# нужен и часто не установлен.
SUDO=""
if [ "$(id -u)" != "0" ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "Нужен root или sudo: ставятся системные пакеты." >&2
        exit 1
    fi
fi

say "система"
. /etc/os-release 2>/dev/null || true
echo "${PRETTY_NAME:-неизвестно}"
if ! command -v apt-get >/dev/null 2>&1; then
    echo "Это не Debian/Ubuntu — apt-get не найден." >&2
    echo "Поставьте python3 (3.11+), python3-venv, git и curl сами," >&2
    echo "а дальше запускайте deploy/install-server.sh." >&2
    exit 1
fi

say "пакеты"
# python3-venv отдельным пакетом — это особенность Debian и Ubuntu: без него
# `python3 -m venv` падает с советом поставить пакет, которого нет.
# ca-certificates нужен curl и git для проверки TLS, tzdata — для пояса.
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates tzdata
python3 --version
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python старее 3.11 — зависимости не поставятся." >&2
    echo "На Ubuntu 22.04 и новее: apt install python3.12 python3.12-venv" >&2
    exit 1
fi

say "часовой пояс"
# Cron про пояса не знает и работает по времени сервера. Приводим время
# сервера к нужному один раз — тогда и переходы на летнее время отработают
# сами, а пересчитывать часы в строках расписания не придётся.
NOW_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "")
if [ "$NOW_TZ" = "$WANT_TZ" ]; then
    echo "уже $WANT_TZ, сейчас $(date '+%H:%M %Z')"
elif command -v timedatectl >/dev/null 2>&1 && $SUDO timedatectl set-timezone "$WANT_TZ" 2>/dev/null; then
    echo "было ${NOW_TZ:-неизвестно}, стало $WANT_TZ, сейчас $(date '+%H:%M %Z')"
else
    # В контейнере без systemd timedatectl не работает — ставим ссылку сами.
    if [ -f "/usr/share/zoneinfo/$WANT_TZ" ]; then
        $SUDO ln -sf "/usr/share/zoneinfo/$WANT_TZ" /etc/localtime
        echo "$WANT_TZ" | $SUDO tee /etc/timezone >/dev/null 2>&1 || true
        echo "было ${NOW_TZ:-неизвестно}, стало $WANT_TZ, сейчас $(date '+%H:%M %Z')"
    else
        echo "ВНИМАНИЕ: пояс сменить не вышло, сейчас $(date '+%H:%M %Z')."
        echo "Расписание встанет по этому времени — поправьте вручную:"
        echo "    sudo timedatectl set-timezone $WANT_TZ"
    fi
fi

say "код"
if [ -d "$DIR/.git" ]; then
    echo "уже есть в $DIR — обновляю"
    git -C "$DIR" fetch --quiet origin "$BRANCH"
    git -C "$DIR" checkout --quiet "$BRANCH"
    git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
else
    $SUDO mkdir -p "$(dirname "$DIR")"
    if ! git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"; then
        echo >&2
        echo "Не склонировалось. Если репозиторий закрытый, перенесите его" >&2
        echo "с рабочей машины и запустите установку оттуда:" >&2
        echo "    scp -r ~/Desktop/inspobot ПОЛЬЗОВАТЕЛЬ@СЕРВЕР:$DIR" >&2
        echo "    ssh ПОЛЬЗОВАТЕЛЬ@СЕРВЕР 'cd $DIR && sh deploy/install-server.sh'" >&2
        exit 1
    fi
    echo "склонировано в $DIR (ветка $BRANCH)"
fi
git -C "$DIR" log --oneline -1

say "установка"
cd "$DIR"
exec sh deploy/install-server.sh "$@"
