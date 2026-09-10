# inspobot

Каждое утро присылает в Telegram подборку интерфейсных референсов из
[Mobbin](https://mobbin.com) — с картинками, ссылками и коротким разбором:
что на экране сделано и почему это работает.

Это дайджест, а не чат-бот. Между отправками ничего не крутится: cron будит
скрипт, тот собирает письмо и завершается.

## Состав настраивается один раз

`python -m inspobot.setup_cli` проводит по шагам и сохраняет профиль. Дайджест
состоит из **блоков**, у каждого свои атрибуты:

| Ось | Варианты |
|---|---|
| Что искать | экраны · флоу (многошаговые сценарии) · секции сайта |
| Платформа | мобилка · десктоп |
| Аудитория | B2B · B2C |
| Темы | 8–10 на каждое сочетание, чередуются по дням |

По умолчанию три блока: мобильные экраны для B2C, десктопные для B2B и один
многошаговый сценарий. Посмотреть, что уйдёт сегодня, не тратя запрос к API:

```bash
python -m inspobot.daily --plan
```

**Про B2B/B2C честно:** такого фильтра у Mobbin нет. Разделение сделано двумя
наборами тем с разными формулировками запроса — у B2B спрашивается про биллинг,
роли и интеграции, у B2C про пейволл, ленту и оформление заказа. Работает
хорошо, но это не жёсткая фильтрация.

## Как работает

За референсами ходит Claude через **MCP-коннектор** Messages API: соединение с
удалённым MCP-сервером Mobbin держит сторона Anthropic. Один запрос покрывает
все блоки — Claude вызывает нужный инструмент Mobbin для каждого, **смотрит на
сами картинки**, отбирает и возвращает JSON по схеме.

```
cron 11:00 → daily.py → Claude ⇄ Mobbin MCP → JSON → Telegram
                 ├── profile.json: из чего состоит дайджест
                 └── SQLite: что уже присылали
```

Показанные экраны запоминаются и уходят в `exclude_screen_ids` следующего
поиска — повторов не будет. Подробнее — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Быстрый старт

```bash
git clone https://github.com/Vladislavkotyreb/inspobot
cd inspobot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp .env.example .env && nano .env      # токен бота, ключ Anthropic
.venv/bin/python -m inspobot.auth_cli  # один раз: вход в Mobbin через браузер
.venv/bin/python -m inspobot.doctor    # все ли доступы на месте

.venv/bin/python -m inspobot.setup_cli       # собрать состав дайджеста
.venv/bin/python -m inspobot.daily --dry-run # посмотреть в консоли
.venv/bin/python -m inspobot.daily --force   # отправить в чат
```

Дальше — расписание. На своём Маке хватит одной команды:

```bash
./deploy/install-macos.sh
```

На сервере — строка в cron или systemd-таймер, оба в [docs/DEPLOY.md](docs/DEPLOY.md).

## Команды

| Команда | Что делает |
|---|---|
| `inspobot.setup_cli` | собрать состав дайджеста по шагам |
| `inspobot.daily` | отправить подборку (так его зовёт cron) |
| `inspobot.daily --plan` | что уйдёт сегодня, без обращения к API |
| `inspobot.daily --dry-run` | собрать и показать в консоли, не отправляя |
| `inspobot.daily --force` | отправить, даже если сегодня уже слали |
| `inspobot.doctor` | проверить доступы, подсказать `chat_id` |
| `inspobot.auth_cli` | вход в Mobbin (нужен один раз) |

## Требования

- Python 3.11+ (на macOS системный `python3` — 3.9, на нём `anthropic` не
  ставится вовсе: поставьте с [python.org](https://www.python.org/downloads/macos/))
- подписка Mobbin **Pro** и выше (MCP на бесплатном тарифе не работает)
- ключ [Anthropic API](https://console.anthropic.com), созданный внутри workspace
- бот в Telegram от [@BotFather](https://t.me/BotFather)

Из пакетов — только `anthropic` и `httpx`.

## Проверки

```bash
./run-checks.sh
```

86 тестов: профиль и ротация тем, сборка запроса под каждый инструмент Mobbin,
разбор и валидация ответа модели, форматирование под лимиты Telegram,
дедупликация в SQLite, самопроверка доступов с разбором частых отказов API и
вся оркестровка с подменёнными Claude и Telegram. Ключи и сеть не нужны.

**Что не проверено:** отправка в Telegram и запросы к Mobbin из среды
разработки — `api.telegram.org` и `mobbin.com` оттуда закрыты. Первый реальный
прогон — `--dry-run`.

## Настройки

Всё через окружение или `.env`, полный список — в [`.env.example`](.env.example).

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `INSPOBOT_TZ` | `Europe/Moscow` | пояс для «сегодня» |
| `ANTHROPIC_MODEL` | `claude-opus-5` | `claude-sonnet-5` втрое дешевле |
| `INSPOBOT_EFFORT` | `high` | `medium` — быстрее и дешевле |
| `INSPOBOT_PROFILE` | `var/profile.json` | где лежит состав дайджеста |

Темы и оси правятся в [`inspobot/catalog.py`](inspobot/catalog.py), правила
отбора — в [`inspobot/prompt.py`](inspobot/prompt.py), вид сообщения — в
[`inspobot/render.py`](inspobot/render.py).
