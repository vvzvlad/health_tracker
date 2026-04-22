from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import aiosqlite
from loguru import logger


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                timezone   TEXT NOT NULL DEFAULT '+03:00',
                created_at INTEGER NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                name               TEXT NOT NULL,
                remind_time        TEXT,
                last_reminded_date TEXT,
                created_at         INTEGER NOT NULL,
                UNIQUE(user_id, name)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                metric_id   INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
                value       INTEGER NOT NULL CHECK(value BETWEEN 0 AND 5),
                recorded_at INTEGER NOT NULL
            )
        """)
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_records_metric ON records(metric_id, recorded_at)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_user ON metrics(user_id)")
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # --- Users ---

    async def get_or_create_user(self, user_id: int, timezone: str) -> dict:
        now = int(datetime.now(tz=dt_timezone.utc).timestamp())
        await self._db.execute(
            "INSERT OR IGNORE INTO users (user_id, timezone, created_at) VALUES (?, ?, ?)",
            (user_id, timezone, now),
        )
        await self._db.commit()
        async with self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)

    async def update_user_timezone(self, user_id: int, timezone: str) -> bool:
        cur = await self._db.execute(
            "UPDATE users SET timezone = ? WHERE user_id = ?", (timezone, user_id)
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def get_user(self, user_id: int) -> dict | None:
        async with self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # --- Metrics ---

    async def add_metric(self, user_id: int, name: str, remind_time: str | None) -> dict | None:
        now = int(datetime.now(tz=dt_timezone.utc).timestamp())
        name = name.lower()
        try:
            await self._db.execute(
                "INSERT INTO metrics (user_id, name, remind_time, created_at) VALUES (?, ?, ?, ?)",
                (user_id, name, remind_time, now),
            )
            await self._db.commit()
        except aiosqlite.IntegrityError:
            return None
        async with self._db.execute(
            "SELECT * FROM metrics WHERE user_id = ? AND name = ?", (user_id, name)
        ) as cur:
            row = await cur.fetchone()
            result = dict(row)
        logger.info("Added metric '{}' for user {}", name, user_id)
        return result

    async def get_metrics(self, user_id: int) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM metrics WHERE user_id = ? ORDER BY name", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_metric_by_name(self, user_id: int, name: str) -> dict | None:
        name = name.lower()
        async with self._db.execute(
            "SELECT * FROM metrics WHERE user_id = ? AND name = ?", (user_id, name)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_metric_by_id(self, metric_id: int) -> dict | None:
        async with self._db.execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def delete_metric(self, user_id: int, name: str) -> bool:
        name = name.lower()
        cur = await self._db.execute(
            "DELETE FROM metrics WHERE user_id = ? AND name = ?", (user_id, name)
        )
        await self._db.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("Deleted metric '{}' for user {}", name.lower(), user_id)
        return deleted

    async def get_all_metrics_with_reminder(self) -> list[dict]:
        async with self._db.execute(
            """
            SELECT metrics.*, users.timezone
            FROM metrics
            JOIN users ON metrics.user_id = users.user_id
            WHERE metrics.remind_time IS NOT NULL
            """
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def set_last_reminded_date(self, metric_id: int, date_str: str) -> None:
        await self._db.execute(
            "UPDATE metrics SET last_reminded_date = ? WHERE id = ?", (date_str, metric_id)
        )
        await self._db.commit()

    # --- Records ---

    async def add_record(self, user_id: int, metric_id: int, value: int) -> dict:
        recorded_at = int(datetime.now(tz=dt_timezone.utc).timestamp())
        cur = await self._db.execute(
            "INSERT INTO records (user_id, metric_id, value, recorded_at) VALUES (?, ?, ?, ?)",
            (user_id, metric_id, value, recorded_at),
        )
        await self._db.commit()
        record_id = cur.lastrowid
        logger.info("Added record: user={} metric_id={} value={}", user_id, metric_id, value)
        return {"id": record_id, "user_id": user_id, "metric_id": metric_id, "value": value, "recorded_at": recorded_at}

    async def get_records(self, user_id: int, metric_name: str | None = None) -> list[dict]:
        if metric_name is not None:
            sql = """
                SELECT metrics.name AS metric_name, records.value, records.recorded_at
                FROM records
                JOIN metrics ON records.metric_id = metrics.id
                WHERE records.user_id = ? AND metrics.name = lower(?)
                ORDER BY records.recorded_at ASC
            """
            params = (user_id, metric_name)
        else:
            sql = """
                SELECT metrics.name AS metric_name, records.value, records.recorded_at
                FROM records
                JOIN metrics ON records.metric_id = metrics.id
                WHERE records.user_id = ?
                ORDER BY records.recorded_at ASC
            """
            params = (user_id,)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
