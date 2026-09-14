# Установка на хостинг

Нужен Python 3.11 или новее и исходящий HTTPS к `api.anthropic.com`,
`api.mobbin.com` и `api.telegram.org`. Больше ничего: ни базы, ни веб-сервера —
состояние лежит в файле SQLite.

## 0. Разведка: годится ли сервер

Прежде чем что-то ставить, проверьте сервер одним скриптом. Он смотрит версию
Python, доступность трёх нужных хостов, наличие cron и время сервера:

```bash
ssh USER@СЕРВЕР
curl -fsSLO https://raw.githubusercontent.com/Vladislavkotyreb/inspobot/claude/inspobot-mobbin-msp-v2p3u1/deploy/check-server.sh
sh check-server.sh
```

Что должно получиться:

```
версия подходит (нужен 3.11+)
api.anthropic.com      доступен (401)
api.mobbin.com         доступен (401)
api.telegram.org       доступен (404)
```

(Ссылка ведёт на ветку `claude/inspobot-mobbin-msp-v2p3u1` — сейчас она в
репозитории единственная и стоит основной. Переименуете её в `main` —
поправьте адрес.)

Коды `401` и `404` здесь — хороший знак: до хоста дошли, просто без ключа.
**`НЕДОСТУПЕН` у `api.anthropic.com` означает, что дальше идти незачем** —
бот на этом сервере работать не будет, сколько его ни настраивай. Это самая
частая история с российскими хостингами; решается сменой площадки или
маршрута, но не настройками бота.

`crontab: НЕТ` — не приговор: расписание можно повесить на systemd-таймер
или на планировщик в панели хостинга.

## 1. Установка

```bash
git clone https://github.com/Vladislavkotyreb/inspobot ~/inspobot
cd ~/inspobot
sh deploy/install-server.sh
```

Скрипт найдёт подходящий Python, соберёт окружение, поставит зависимости и
создаст `.env` из шаблона — после чего остановится и попросит его заполнить.

## 2. Секреты

```bash
nano ~/inspobot/.env     # TELEGRAM_BOT_TOKEN и ANTHROPIC_API_KEY
```

Токен Mobbin можно получить прямо на сервере — в два шага, см.
[MOBBIN_AUTH.md](MOBBIN_AUTH.md):

```bash
.venv/bin/python -m inspobot.auth_cli --start
# открыть ссылку в браузере, войти, скопировать адрес из адресной строки
.venv/bin/python -m inspobot.auth_cli --finish 'ВСТАВИТЬ_АДРЕС'
```

Либо скопировать готовый файл с рабочей машины:

```bash
# с ноутбука, не с сервера
scp ~/Desktop/inspobot/var/mobbin_token.json USER@СЕРВЕР:~/inspobot/var/
```

Заодно перенесите список получателей и расписание, если настраивали их локально:

```bash
scp ~/Desktop/inspobot/var/chats.txt USER@СЕРВЕР:~/inspobot/var/
scp ~/Desktop/inspobot/var/profile.json USER@СЕРВЕР:~/inspobot/var/
```

### История: не потерять то, что уже накопилось

Если бот до этого работал в GitHub Actions, вся память живёт в артефакте —
показанные экраны, оценки находок и номера сообщений, на которых стоит топ.
Без неё топ за месяц начнётся с нуля.

Забрать: **Actions → последний успешный запуск → внизу страницы Artifacts →
`inspobot-state`**. Скачается zip, внутри `inspobot.sqlite3`. Распаковать и
положить на сервер:

```bash
# на своей машине
unzip -o ~/Downloads/inspobot-state.zip -d /tmp/inspobot-state
scp /tmp/inspobot-state/inspobot.sqlite3 USER@СЕРВЕР:~/inspobot/var/
```

Если истории не жалко — шаг можно пропустить, бот заведёт пустую базу сам.

Потом запустите установку ещё раз, теперь она дойдёт до конца:

```bash
cd ~/inspobot && sh deploy/install-server.sh --cron
```

Флаг `--cron` сразу добавит строку в расписание. Без него скрипт просто
напечатает её, чтобы вы вставили сами.

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

Затем — состав дайджеста:

```bash
.venv/bin/python -m inspobot.setup_cli   # пройти шаги и сохранить профиль
.venv/bin/python -m inspobot.daily --plan  # проверить, что получилось
```

Дальше сама подборка:

```bash
.venv/bin/python -m inspobot.daily --dry-run --verbose   # в консоль, без отправки
.venv/bin/python -m inspobot.daily --force               # реальная отправка в чат
```

`--force` нужен потому, что вторая подборка за день не отправляется: защита от
двойного срабатывания cron.

## 4. Расписание: два способа

### Часовой пояс — сначала он

Cron живёт по времени сервера и про пояса ничего не знает: строка «0 11»
отработает в 11:00 **сервера**. У большинства провайдеров это UTC.

Пересчитывать час один раз — плохая идея: при переходе на летнее время он
уедет. Правильнее привести пояс сервера к нужному, тогда и переходы
отработают сами:

```bash
sudo timedatectl set-timezone Europe/Moscow
date    # проверить
```

`install-server.sh --cron` сверяет пояс сервера с `INSPOBOT_TZ` из `.env` и
**отказывается ставить расписание**, пока они не совпадут, — чтобы письмо не
приходило не вовремя.

### Вариант А — системный cron (проще)

`install-server.sh --cron` делает это сам. Вручную — `crontab -e` и строка из
[`deploy/crontab.example`](../deploy/crontab.example).

**Проверьте пояс сервера:** `date`. Скрипт печатает его время, но час в строке
не пересчитывает — cron не знает про пояса. Если сервер живёт в UTC, 11:00 МСК
это `0 8 * * *`.

### Вариант Б — systemd

```bash
sudo cp deploy/inspobot-daily.service deploy/inspobot-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inspobot-daily.timer
systemctl list-timers inspobot-daily
```

Пояс задан прямо в таймере, системное время сервера трогать не нужно
(systemd 252+).

### Вариант Г — на своём Маке, без сервера

Если бот живёт прямо на ноутбуке:

```bash
cd ~/Desktop/inspobot
./deploy/install-macos.sh
```

Скрипт сам подставит пути, возьмёт время из `.env` и зарегистрирует задачу в
launchd. Проверить: `launchctl list | grep com.inspobot.daily`. Лог —
`var/launchd.log`. Выключить: `./deploy/install-macos.sh --remove`.

cron на macOS для этого не годится: если ноутбук спал в назначенный час,
задача просто пропускается. launchd в такой ситуации запускает её при
пробуждении — подборка придёт позже 11:00, но придёт. А если Мак был выключен
целиком, дня не будет вовсе: для гарантии нужен сервер.

## 5. Что где лежит

```
/opt/inspobot/.env                    секреты, права 600
/opt/inspobot/var/mobbin_token.json   токены Mobbin, права 600
/opt/inspobot/var/profile.json        состав дайджеста
/opt/inspobot/var/inspobot.sqlite3    что уже присылали
/opt/inspobot/var/cron.log            лог, если запускаете через cron
```

Каталог `var/` в git не попадает. Бэкапить осмысленно только его.

## 6. Обновление

```bash
cd /opt/inspobot && git pull && .venv/bin/pip install -r requirements.txt
```

## Диагностика

| Симптом | Куда смотреть |
|---|---|
| Утром ничего не пришло | `inspobot.doctor`, затем `var/cron.log` или `journalctl -u inspobot-daily` |
| «Нет файла с токенами Mobbin» | [MOBBIN_AUTH.md](MOBBIN_AUTH.md) |
| Нужен совсем подробный лог | `--verbose` безопасен. А вот `ANTHROPIC_LOG=debug` печатает тело запроса вместе с токеном Mobbin — такой лог никому не пересылайте |
| Пришёл текст без картинок | Telegram не смог забрать превью; ссылки на Mobbin в сообщениях остаются рабочими |
| «Подборка на … уже уходила» | нормальное поведение; для повтора `--force` |
| Хочется отправить заново | `.venv/bin/python -m inspobot.daily --force` |
