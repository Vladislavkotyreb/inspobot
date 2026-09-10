# Установка на хостинг

Нужен Python 3.11 или новее и исходящий HTTPS к `api.anthropic.com`,
`api.mobbin.com` и `api.telegram.org`. Больше ничего: ни базы, ни веб-сервера —
состояние лежит в файле SQLite.

## 1. Код и зависимости

```bash
ssh USER@СЕРВЕР
python3 --version        # нужен 3.11+; SDK anthropic не ставится на 3.9
git clone https://github.com/Vladislavkotyreb/inspobot /opt/inspobot
cd /opt/inspobot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python --version
```

Если `python3` оказался старым, поставьте новый и соберите окружение им:
на macOS `brew install python@3.12`, затем
`"$(brew --prefix)/bin/python3.12" -m venv .venv`; на Debian/Ubuntu
`apt install python3.12-venv`, затем `python3.12 -m venv .venv`. Признак того,
что этот шаг пропущен, — `ModuleNotFoundError: No module named 'httpx'`:
pip не смог поставить anthropic и не поставил вообще ничего.

На ISPmanager каталог обычно живёт в `/var/www/USER/data/inspobot` — путь
подставьте свой, дальше по тексту он везде `/opt/inspobot`.

## 2. Настройки

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Что заполнить:

| Переменная | Где взять |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | напишите боту любое сообщение, затем `inspobot.doctor` покажет id |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API keys. Ключ лучше создавать **внутри workspace**: ключ уровня организации API отклоняет, пока не задан `ANTHROPIC_WORKSPACE_ID` |

Токен Mobbin получается отдельно — см. [MOBBIN_AUTH.md](MOBBIN_AUTH.md).

## 3. Проверка до расписания

Сначала доступы — команда проверяет их по отдельности, поэтому сразу видно,
что именно чинить:

```bash
.venv/bin/python -m inspobot.doctor
```

```
✓ Telegram: бот @your_bot (id 1234567890)
! chat_id: не задан. Подходит: 123456789 (private, Влад)
✓ Anthropic: модель доступна: claude-opus-5
✓ Mobbin: токен есть, обновляется сам (осталось 58 мин)
```

Допишите `TELEGRAM_CHAT_ID` в `.env` и прогоните ещё раз — теперь всё зелёное.
`--send-test` дополнительно напишет в чат.

Дальше сама подборка:

```bash
.venv/bin/python -m inspobot.daily --dry-run --verbose   # в консоль, без отправки
.venv/bin/python -m inspobot.daily --force               # реальная отправка в чат
```

`--force` нужен потому, что вторая подборка за день не отправляется: защита от
двойного срабатывания cron.

## 4. Расписание: два способа

### Вариант А — системный cron (проще)

```bash
crontab -e
```

и строка из [`deploy/crontab.example`](../deploy/crontab.example). Проверьте
пояс сервера: `timedatectl` или `date`. Если сервер в UTC, 11:00 МСК — это
`0 8 * * *`.

### Вариант Б — systemd

```bash
sudo cp deploy/inspobot-daily.service deploy/inspobot-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inspobot-daily.timer
systemctl list-timers inspobot-daily
```

Пояс задан прямо в таймере, системное время сервера трогать не нужно
(systemd 252+).

### Вариант В — бот-демон со своим расписанием

Если хочется, чтобы работали команды `/now` и `/topics`, держите процесс живым:

```bash
sudo cp deploy/inspobot-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inspobot-bot
journalctl -u inspobot-bot -f
```

Тогда cron и таймер не нужны: расписание внутри процесса. **Не включайте
таймер и демон одновременно** — вторая подборка всё равно не уйдёт, но в логах
будут лишние ошибки.

## 5. Что где лежит

```
/opt/inspobot/.env                    секреты, права 600
/opt/inspobot/var/mobbin_token.json   токены Mobbin, права 600
/opt/inspobot/var/inspobot.sqlite3    что уже присылали
/opt/inspobot/var/cron.log            лог, если запускаете через cron
```

Каталог `var/` в git не попадает. Бэкапить осмысленно только его.

## 6. Обновление

```bash
cd /opt/inspobot && git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart inspobot-bot   # если работает демоном
```

## Диагностика

| Симптом | Куда смотреть |
|---|---|
| Утром ничего не пришло | `inspobot.doctor`, затем `var/cron.log` или `journalctl -u inspobot-daily` |
| «Нет файла с токенами Mobbin» | [MOBBIN_AUTH.md](MOBBIN_AUTH.md) |
| Пришёл текст без картинок | Telegram не смог забрать превью; ссылки на Mobbin в сообщениях остаются рабочими |
| «Подборка на … уже уходила» | нормальное поведение; для повтора `--force` |
| Хочется отправить заново | `.venv/bin/python -m inspobot.daily --force` |
