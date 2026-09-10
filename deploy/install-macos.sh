#!/usr/bin/env sh
# Ставит ежедневный запуск подборки через launchd (расписание в macOS).
#
#   ./deploy/install-macos.sh            # включить, время берётся из .env
#   ./deploy/install-macos.sh --remove   # выключить
#
# cron на macOS для этого не годится: он не запускает задачу, если ноутбук
# спал в назначенный час. launchd в такой ситуации запускает её при
# пробуждении — подборка придёт с опозданием, но придёт.

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.inspobot.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

unload() {
    launchctl unload "$PLIST" 2>/dev/null || true
}

if [ "$1" = "--remove" ]; then
    unload
    rm -f "$PLIST"
    echo "Расписание выключено."
    exit 0
fi

if [ ! -x "$DIR/.venv/bin/python" ]; then
    echo "Нет $DIR/.venv/bin/python — сначала соберите окружение." >&2
    exit 1
fi

# Время из .env, по умолчанию 11:00.
read_env() {
    [ -f "$DIR/.env" ] || return 0
    sed -n "s/^$1=\([0-9][0-9]*\).*/\1/p" "$DIR/.env" | tail -1
}
HOUR="$(read_env INSPOBOT_HOUR)"; HOUR="${HOUR:-11}"
MINUTE="$(read_env INSPOBOT_MINUTE)"; MINUTE="${MINUTE:-0}"

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/var"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/.venv/bin/python</string>
        <string>-m</string>
        <string>inspobot.daily</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$DIR/var/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/var/launchd.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLISTEOF

unload
launchctl load -w "$PLIST"

printf 'Готово. Подборка будет приходить в %02d:%02d.\n' "$HOUR" "$MINUTE"
echo "Лог: $DIR/var/launchd.log"
echo "Проверить, что задача зарегистрирована: launchctl list | grep $LABEL"
echo "Выключить: ./deploy/install-macos.sh --remove"
