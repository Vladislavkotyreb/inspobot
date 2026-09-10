#!/usr/bin/env sh
# Установка на сервер. Запускать из каталога с кодом:
#
#   sh deploy/install-server.sh
#
# Ничего не трогает за пределами каталога проекта и (с флагом --cron)
# пользовательского crontab.

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

# 1. Интерпретатор
PY=""
for CMD in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$CMD" >/dev/null 2>&1 &&
       "$CMD" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$CMD"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Не нашёл Python 3.11+. Поставьте его и повторите." >&2
    echo "Debian/Ubuntu: apt install python3.12 python3.12-venv" >&2
    exit 1
fi
echo "Python: $($PY --version 2>&1)"

# 2. Окружение
if [ ! -x .venv/bin/python ]; then
    "$PY" -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "Зависимости установлены."

# 3. Секреты
mkdir -p var
chmod 700 var
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo
    echo "Создан .env — заполните TELEGRAM_BOT_TOKEN и ANTHROPIC_API_KEY:"
    echo "    nano $DIR/.env"
    echo "И перенесите с рабочей машины токен Mobbin:"
    echo "    scp var/mobbin_token.json $(id -un)@СЕРВЕР:$DIR/var/"
    echo
    echo "Потом запустите этот скрипт ещё раз."
    exit 0
fi
chmod 600 .env
[ -f var/mobbin_token.json ] && chmod 600 var/mobbin_token.json

# 4. Проверка доступов
echo
echo "=== проверка доступов ==="
if ! .venv/bin/python -m inspobot.doctor; then
    echo
    echo "Что-то не сошлось — почините и запустите скрипт снова." >&2
    exit 1
fi

# 5. Расписание
HOUR=$(sed -n 's/^INSPOBOT_HOUR=\([0-9][0-9]*\).*/\1/p' .env | tail -1)
HOUR="${HOUR:-11}"
MINUTE=$(sed -n 's/^INSPOBOT_MINUTE=\([0-9][0-9]*\).*/\1/p' .env | tail -1)
MINUTE="${MINUTE:-0}"
LINE="$MINUTE $HOUR * * * cd $DIR && .venv/bin/python -m inspobot.daily >> var/cron.log 2>&1"

echo
if [ "$1" = "--cron" ]; then
    if crontab -l 2>/dev/null | grep -q "inspobot.daily"; then
        echo "Строка про inspobot уже есть в crontab — не трогаю."
    else
        (crontab -l 2>/dev/null; echo "$LINE") | crontab -
        echo "Добавлено в crontab:"
        echo "    $LINE"
    fi
    echo
    echo "ВНИМАНИЕ: время сервера — $(date '+%H:%M %Z')."
    echo "Если это не ваш пояс, поправьте час в строке crontab вручную."
else
    echo "Готово. Осталось расписание — добавьте строку в crontab -e:"
    echo
    echo "    $LINE"
    echo
    echo "Время сервера сейчас: $(date '+%H:%M %Z') — сверьте с нужным поясом."
    echo "Или запустите: sh deploy/install-server.sh --cron"
fi
