# inspobot

Каждое утро в 11:00 присылает в Telegram подборку интерфейсных референсов из
[Mobbin](https://mobbin.com): пять мобильных экранов и пять десктопных, с
картинками, ссылками и коротким разбором — что на экране сделано и почему это
работает.

Тема дня меняется по ротации (онбординг, пейволл, настройки, дашборд, таблицы,
редактор, биллинг…), а показанные экраны запоминаются и больше не предлагаются.

## Как работает

Бот — обычный Python-процесс на вашем хостинге. За референсами он ходит в
**MCP-сервер Mobbin**, но не сам: Claude подключается к Mobbin через
MCP-коннектор Messages API, вызывает поиск, **смотрит на сами картинки**,
отбирает лучшее и возвращает готовый JSON. Бот из этого JSON собирает
сообщения и отправляет их в Telegram.

```
cron 11:00 → daily.py → Claude ⇄ Mobbin MCP → JSON → Telegram
                 └── SQLite: что уже присылали
```

Подробнее — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Быстрый старт

```bash
git clone https://github.com/Vladislavkotyreb/inspobot
cd inspobot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp .env.example .env && nano .env      # токен бота, ключ Anthropic
.venv/bin/python -m inspobot.auth_cli  # один раз: вход в Mobbin через браузер

.venv/bin/python -m inspobot.doctor            # все ли доступы на месте
.venv/bin/python -m inspobot.daily --dry-run   # посмотреть подборку в консоли
.venv/bin/python -m inspobot.daily --force     # отправить в чат
```

Дальше — расписание. На своём Маке хватит одной команды:

```bash
./deploy/install-macos.sh
```

На сервере — строка в cron, systemd-таймер или демон со своим таймером внутри.
Все варианты в [docs/DEPLOY.md](docs/DEPLOY.md).

Про вход в Mobbin (почему OAuth, а не ключ) — [docs/MOBBIN_AUTH.md](docs/MOBBIN_AUTH.md).

## Команды бота

Работают, если запущен `python -m inspobot.bot`:

| Команда | Что делает |
|---|---|
| `/now` | собрать и прислать подборку прямо сейчас |
| `/topics` | темы на сегодня и завтра |
| `/id` | id текущего чата — нужен при первичной настройке |
| `/help` | справка |

Отдельно от бота, из консоли: `python -m inspobot.doctor` — проверяет токен
Telegram, ключ Anthropic и токен Mobbin по отдельности и подсказывает `chat_id`,
если он ещё не задан. С `--send-test` ещё и пишет в чат.

Если расписание живёт в cron, демон не нужен — но и команд тогда нет.

## Требования

- Python 3.11+ (на macOS системный `python3` — 3.9, на нём `anthropic` не
  ставится вовсе: `brew install python@3.12`)
- подписка Mobbin **Pro** и выше (MCP на бесплатном тарифе не работает)
- ключ [Anthropic API](https://console.anthropic.com)
- бот в Telegram от [@BotFather](https://t.me/BotFather)

Из пакетов — только `anthropic` и `httpx`.

## Проверки

```bash
./run-checks.sh
```

61 тест: ротация тем, разбор и валидация ответа модели, форматирование под
лимиты Telegram, дедупликация в SQLite, форма запроса к Messages API,
самопроверка доступов с разбором частых отказов API и вся оркестровка с подменёнными Claude и Telegram.
Ключи и сеть не нужны.

**Что не проверено:** живых запросов к Mobbin, Anthropic и Telegram не было —
у автора кода не было ни ваших ключей, ни доступа к mobbin.com. Первый реальный
прогон — `--dry-run`, он покажет, что OAuth и MCP-коннектор сошлись.

## Настройки

Всё через окружение или `.env`, полный список — в
[`.env.example`](.env.example). Самое частое:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `INSPOBOT_PICKS_PER_PLATFORM` | `5` | экранов на площадку |
| `INSPOBOT_HOUR` / `INSPOBOT_TZ` | `11` / `Europe/Moscow` | время для режима демона |
| `ANTHROPIC_MODEL` | `claude-opus-5` | `claude-sonnet-5` заметно дешевле |
| `INSPOBOT_EFFORT` | `high` | `medium` — быстрее и дешевле |

Темы правятся в [`inspobot/topics.py`](inspobot/topics.py), правила отбора — в
[`inspobot/prompt.py`](inspobot/prompt.py), вид сообщения — в
[`inspobot/render.py`](inspobot/render.py).
