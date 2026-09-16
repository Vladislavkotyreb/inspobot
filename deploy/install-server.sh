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
read_env() {
    VALUE=$(sed -n "s/^$1=//p" .env | tail -1)
    # Хвостовой комментарий и кавычки: «INSPOBOT_HOUR=11  # утро» иначе
    # становится часом «11  # утро» и ломает строку crontab целиком.
    VALUE=${VALUE%%#*}
    VALUE=$(printf '%s\n' "$VALUE" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^["'"'"']//; s/["'"'"']$//')
    echo "${VALUE:-$2}"
}

HOUR=$(read_env INSPOBOT_HOUR 11)
MINUTE=$(read_env INSPOBOT_MINUTE 0)
FEED_HOUR=$(read_env INSPOBOT_FEED_HOUR 9)
FEED_MINUTE=$(read_env INSPOBOT_FEED_MINUTE 0)
WANT_TZ=$(read_env INSPOBOT_TZ Europe/Moscow)

DAILY_LINE="$MINUTE $HOUR * * * cd $DIR && .venv/bin/python -m inspobot.daily >> var/cron.log 2>&1"
FEED_LINE="$FEED_MINUTE $FEED_HOUR * * * cd $DIR && .venv/bin/python -m inspobot.feed >> var/cron.log 2>&1"

# Cron живёт по времени сервера и ничего не знает про пояса. Если пояс
# сервера не тот, в котором вы ждёте письмо, строка «0 11» отработает не в
# 11:00. Пересчитывать час один раз нельзя: при переходе на летнее время он
# уедет. Правильный путь — привести пояс сервера к нужному, тогда и переходы
# отработают сами.
SERVER_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "")
echo
if [ -n "$SERVER_TZ" ] && [ "$SERVER_TZ" != "$WANT_TZ" ]; then
    echo "ВНИМАНИЕ: пояс сервера — $SERVER_TZ, а письма ждём по $WANT_TZ."
    echo "Cron про пояса не знает, поэтому приведите время сервера к нужному:"
    echo
    echo "    sudo timedatectl set-timezone $WANT_TZ"
    echo
    echo "и запустите этот скрипт снова. Сейчас на сервере $(date '+%H:%M %Z')."
    TZ_MISMATCH=1
else
    echo "Пояс сервера: ${SERVER_TZ:-неизвестен}, сейчас $(date '+%H:%M %Z') — совпадает с ожидаемым."
    TZ_MISMATCH=0
fi

# Лента ходит по своим адресам, и ни один из них не совпадает с адресами
# подборки. Сказать про это здесь дешевле, чем утром разбираться, почему
# письмо пустое.
echo
echo "=== источники ленты ==="
.venv/bin/python -m inspobot.feed --list 2>&1 | sed -n '1,4p'
echo "Полная проверка (ходит в сеть): .venv/bin/python -m inspobot.feed --probe"

add_cron() {
    # $1 — строка, $2 — по чему искать уже стоящую
    if crontab -l 2>/dev/null | grep -q "$2"; then
        echo "  уже есть: $2"
        return 0
    fi
    (crontab -l 2>/dev/null; echo "$1") | crontab - 2>/dev/null || true
    # Проверяем, а не верим на слово. `crontab -` умеет завершиться нулём и
    # ничего не записать — например, пока каталог спула ещё не создан после
    # свежей установки пакета cron. Рапорт «добавлено» там, где ничего не
    # добавилось, означает молчащего бота и неделю поисков причины.
    if crontab -l 2>/dev/null | grep -q "$2"; then
        echo "  добавлено: $1"
    else
        echo "  НЕ ЗАПИСАЛОСЬ: $2" >&2
        echo "  Добавьте руками через crontab -e:" >&2
        echo "    $1" >&2
        CRON_FAILED=1
    fi
}

echo
if [ "$1" = "--cron" ]; then
    if [ "$TZ_MISMATCH" = "1" ]; then
        echo "Расписание не ставлю, пока пояса не сойдутся — иначе письма придут не вовремя."
        echo "Строки, которые нужны после смены пояса:"
        echo "    $DAILY_LINE"
        echo "    $FEED_LINE"
        exit 0
    fi
    echo "=== расписание ==="
    CRON_FAILED=0
    if ! command -v crontab >/dev/null 2>&1; then
        echo "  crontab не установлен: sudo apt install cron" >&2
        echo "  Нужные строки:" >&2
        echo "    $DAILY_LINE" >&2
        echo "    $FEED_LINE" >&2
        CRON_FAILED=1
    else
        add_cron "$DAILY_LINE" "inspobot.daily"
        add_cron "$FEED_LINE" "inspobot.feed"
    fi
    echo
    echo "Проверить: crontab -l"
    echo "Лог запусков: $DIR/var/cron.log"
    if [ "$CRON_FAILED" = "1" ]; then
        echo
        echo "Расписание встало не полностью — см. строки выше." >&2
    fi
    # Служба кнопок: сервер работает круглосуточно, поэтому нажатия
    # принимает он сам — внешний ретранслятор не нужен.
    # Наличия команды мало: в контейнере без systemd как init она есть, но
    # падает на «Failed to connect to bus», и под set -e это обрывает всю
    # установку — уже на готовом расписании, что выглядит как полный провал.
    if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
        sed "s|/root/inspobot|$DIR|g" deploy/inspobot-listener.service \
            > /etc/systemd/system/inspobot-listener.service
        systemctl daemon-reload || true
        systemctl enable --now inspobot-listener >/dev/null 2>&1 || true
        echo
        echo "Служба кнопок: $(systemctl is-active inspobot-listener 2>/dev/null || echo нет)"
        echo "Логи: journalctl -u inspobot-listener -f"
        if ! grep -q '^INSPOBOT_TOP_BUTTONS=1' .env; then
            echo
            echo "Кнопки под подборкой пока выключены. Включить:"
            echo "    sed -i 's|^#* *INSPOBOT_TOP_BUTTONS=.*|INSPOBOT_TOP_BUTTONS=1|' .env"
            echo "    grep INSPOBOT_TOP_BUTTONS .env || echo INSPOBOT_TOP_BUTTONS=1 >> .env"
        fi
    fi
else
    echo "Готово. Осталось расписание:"
    echo
    echo "    $DAILY_LINE"
    echo "    $FEED_LINE"
    echo
    echo "Добавить самому: crontab -e. Или запустить: sh deploy/install-server.sh --cron"
fi
