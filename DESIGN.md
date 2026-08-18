# Health Tracker Bot — Архитектура

## Концепция

Телеграм-бот для трекинга произвольных физических/субъективных параметров (настроение, стул, сон, боль и т.д.). Пользователь сам определяет параметры и опционально привязывает к ним напоминания. Значения хранятся как временной ряд: параметр + значение (0–5) + datetime.

---

## Сценарии использования

### Параметр с напоминанием
1. `/add настроение 09:00` — добавить параметр "настроение" с напоминанием в 09:00
2. Каждый день в 09:00 бот спрашивает: "Оцени настроение от 0 до 5"
3. Пользователь отвечает цифрой или нажимает кнопку
4. Запись сохраняется с текущим timestamp

### Параметр без напоминания
1. `/add стул` — добавить параметр без напоминания
2. Пользователь сам пишет `/track стул 3` или через меню
3. Запись сохраняется с текущим timestamp

### Экспорт
- `/export` — получить все данные в CSV

---

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие, помощь |
| `/add <параметр> [HH:MM]` | Добавить параметр (опционально — время напоминания) |
| `/list` | Список параметров и их напоминаний |
| `/delete <параметр>` | Удалить параметр (и все его данные) |
| `/track <параметр> <0-5>` | Вручную зафиксировать значение |
| `/export` | Скачать CSV со всеми записями |
| `/timezone <±HH:MM>` | Установить часовой пояс |

### Ответ на напоминание
Когда бот присылает вопрос — пользователь отвечает:
- Нажатием inline-кнопок 0–5 (удобно)
- Или текстом с цифрой (если открыт диалог)

---

## Схема БД (SQLite)

```sql
-- Пользователи
CREATE TABLE users (
    user_id     INTEGER PRIMARY KEY,
    timezone    TEXT NOT NULL DEFAULT '+03:00',
    created_at  INTEGER NOT NULL
);

-- Параметры (то, что трекаем)
CREATE TABLE metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,           -- "настроение", "стул", "боль в спине"
    remind_time TEXT,                    -- "09:00" или NULL (без напоминания)
    created_at  INTEGER NOT NULL,
    UNIQUE(user_id, name)
);

-- Записи значений (временной ряд)
CREATE TABLE records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    metric_id   INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    value       INTEGER NOT NULL CHECK(value BETWEEN 0 AND 5),
    recorded_at INTEGER NOT NULL         -- unix timestamp UTC
);

CREATE INDEX idx_records_metric ON records(metric_id, recorded_at);
CREATE INDEX idx_metrics_user   ON metrics(user_id);
```

> Нет привязки записи к конкретному «дню напоминания» — только timestamp. Это позволяет трекать когда угодно, независимо от расписания.

---

## Структура проекта

```
health_tracker/
├── main.py                  # точка входа: логирование, init DB, запуск bot + scheduler
├── .env                     # TELEGRAM_BOT_TOKEN, DATABASE_PATH, ...
├── requirements.txt
├── Dockerfile               # образ + HEALTHCHECK (см. «Liveness» ниже)
├── ci/
│   └── smoke.py             # smoke-гейт: гоняет собранный образ между build и push
├── .gitea/workflows/        # PR-гейт (tests.yml) и сборка с публикацией
├── src/
│   ├── settings.py          # pydantic-settings конфиг
│   ├── database.py          # все SQL-операции (aiosqlite)
│   ├── bot.py               # aiogram: хэндлеры команд и callback
│   ├── scheduler.py         # APScheduler: напоминания + heartbeat-job
│   ├── heartbeat.py         # контракт отметки живости (только stdlib)
│   ├── healthcheck.py       # то, что запускает HEALTHCHECK: python -m src.healthcheck
│   ├── logging_setup.py     # единый sink loguru, мост stdlib→loguru, вычистка токена
│   └── utils.py             # парсер часового пояса
└── data/
    └── health.db
```

---

## Компоненты

### `database.py`
Методы:
- `init()` — создание таблиц
- `get_or_create_user(user_id, timezone)` → user
- `add_metric(user_id, name, remind_time)` → metric_id
- `get_metrics(user_id)` → list
- `delete_metric(user_id, name)` → bool
- `add_record(user_id, metric_id, value)` → record_id
- `get_records(user_id, metric_name=None)` → list (для CSV)
- `get_metrics_with_reminder(remind_time_hhmm)` → list (для scheduler)

### `bot.py`
Aiogram 3.x, без LLM. Только явные команды:
- Хэндлеры `/add`, `/list`, `/delete`, `/track`, `/export`, `/timezone`
- `handle_callback` — обработка нажатий кнопок 0–5 при ответе на напоминание
- Состояния (FSM) для ожидания значения после команды `/track`
- `send_reminder(user_id, metric)` — отправить вопрос с inline-кнопками 0–5

### `scheduler.py`
- APScheduler AsyncIOScheduler, job каждую минуту
- Каждую минуту: получить текущее время HH:MM в часовом поясе каждого пользователя
- Найти метрики с `remind_time == current_hhmm`
- Вызвать `bot.send_reminder(...)`
- Дедупликация: не слать повторно если уже отправляли сегодня — хранить `last_reminded_date` в таблице `metrics` или отдельной таблице

#### Дедупликация напоминаний
Добавить в `metrics`:
```sql
last_reminded_date TEXT  -- YYYY-MM-DD в часовом поясе пользователя
```
Перед отправкой проверить — если `last_reminded_date == today`, пропустить.

### `settings.py`
```python
TELEGRAM_BOT_TOKEN: str
TELEGRAM_BOT_API_SERVER: str | None = None   # локальный Bot API server; пустое значение = не задан
DATABASE_PATH: str = "data/health.db"
DEFAULT_TIMEZONE: str = "+03:00"
LOG_LEVEL: str = "INFO"
HEARTBEAT_FILE: str = "/tmp/heartbeat"       # читается напрямую из окружения, мимо Settings
```

### Liveness: `heartbeat.py` + `healthcheck.py`

В образе объявлен `HEALTHCHECK`, который каждые 30 с запускает `python -m src.healthcheck`.
Отдельная 30-секундная job в APScheduler (`touch_heartbeat`) не делает ничего, кроме записи
текущего времени в файл-отметку; проба считает контейнер здоровым, пока отметка не старше 90 с
(три пропущенных тика). Поэтому «healthy» здесь означает «событийный цикл всё ещё крутится», а не
«процесс всё ещё существует»: зависший asyncio-loop сохраняет PID, но перестаёт писать отметку.
Это нужно не само по себе — на контейнере стоит `io.portainer.update.enable`, и откат неудачного
автообновления запускается именно тем, что новый контейнер не дошёл до `healthy`.

Отметка лежит в `/tmp/heartbeat` — в собственном записываемом слое контейнера, а НЕ на томе с
данными (переопределяется `HEARTBEAT_FILE`). Это принципиально: апдейтер пересоздаёт контейнер
поверх ТОГО ЖЕ тома, поэтому отметка, лежащая на томе, досталась бы новому контейнеру от старого, и
первая же проба назвала бы здоровым контейнер, который ещё ничего не сделал. Слой `/tmp` создаётся
пустым при каждом пересоздании — унаследовать отметку оттуда невозможно по построению, проверять
нечего.

Чего проба НЕ проверяет: доходит ли бот до пользователей. После успешного старта aiogram
переспрашивает `getUpdates` вечно, так что поломка на стороне API (409, отозванный токен, пропавший
Bot API server) оставляет цикл живым, отметку свежей, а пробу зелёной. Такое видно только в логе —
за что отвечает `logging_setup.py`, заводящий stdlib-логи библиотек в loguru. Поломка, которая есть
уже НА старте (неверный адрес, отказ в соединении), наоборот, ловится: первый же `set_my_commands`
падает, процесс умирает и до `healthy` не доходит.

---

## Формат CSV-экспорта

```csv
metric,value,recorded_at_utc,recorded_at_local
настроение,4,2026-04-21T09:13:00Z,2026-04-21T12:13:00+03:00
стул,2,2026-04-21T11:05:00Z,2026-04-21T14:05:00+03:00
```

---

## UX: ответ на напоминание

Бот присылает:
```
Как твоё настроение сегодня?
[ 0 ] [ 1 ] [ 2 ] [ 3 ] [ 4 ] [ 5 ]
```
Callback data: `record:{metric_id}:{value}`

После нажатия — сообщение редактируется: `✅ настроение: 3 — сохранено`

---

## Зависимости

```
aiogram==3.x
aiosqlite
apscheduler
pydantic-settings
loguru
python-dotenv
```

---

## Что намеренно не включено

- LLM / NLP — все команды явные и структурированные
- Аутентификация — бот персональный, user_id = auth
- Графики — только CSV сейчас, визуализация потом
- Уведомление если пользователь не ответил — можно добавить позже
