from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src.database import Database
from src.bot import HealthBot
from src.utils import parse_timezone


async def check_reminders(db: Database, bot: HealthBot) -> None:
    try:
        now_utc = datetime.now(timezone.utc)
        all_metrics = await db.get_all_metrics_with_reminder()
        for metric in all_metrics:
            user_tz = parse_timezone(metric["timezone"])
            now_local = now_utc.astimezone(user_tz)
            current_hhmm = now_local.strftime("%H:%M")
            if metric["remind_time"] != current_hhmm:
                continue
            today_local = now_local.strftime("%Y-%m-%d")
            if metric["last_reminded_date"] == today_local:
                continue
            await bot.send_reminder(metric["user_id"], metric)
            await db.set_last_reminded_date(metric["id"], today_local)
            logger.info("Sent reminder: user={} metric={}", metric["user_id"], metric["name"])
    except Exception:
        logger.exception("Error in check_reminders")


class ReminderScheduler:
    def __init__(self, db: Database, bot: HealthBot):
        self.db = db
        self.bot = bot
        self.scheduler = AsyncIOScheduler()

    def start(self):
        self.scheduler.add_job(
            check_reminders,
            trigger="cron",
            minute="*",
            args=[self.db, self.bot],
        )
        self.scheduler.start()
        logger.info("Scheduler started")

    def stop(self):
        self.scheduler.shutdown(wait=False)
