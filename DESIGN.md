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
├── main.py                  # точка входа: init DB, запуск bot + scheduler
├── .env                     # TELEGRAM_BOT_TOKEN, DATABASE_PATH, ...
├── requirements.txt
├── src/
│   ├── settings.py          # pydantic-settings конфиг
│   ├── database.py          # все SQL-операции (aiosqlite)
│   ├── bot.py               # aiogram: хэндлеры команд и callback
│   └── scheduler.py         # APScheduler: ежедневные напоминания
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
DATABASE_PATH: str = "data/health.db"
DEFAULT_TIMEZONE: str = "+03:00"
LOG_LEVEL: str = "INFO"
```

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
