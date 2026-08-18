from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src.database import Database
from src.bot import HealthBot
from src.heartbeat import heartbeat_file, write_heartbeat
from src.utils import parse_timezone


async def check_reminders(db: Database, bot: HealthBot, heartbeat_path: str) -> None:
    # The mark goes FIRST, before the database is touched, and that ordering is the whole
    # design of the probe. This job runs once a minute and its execution is the only
    # evidence the asyncio loop is still turning — which is exactly what the healthcheck is
    # asked to grade. Writing the mark after the query would fold a database problem into
    # the liveness verdict: a locked or unreadable health.db would stop the heartbeat, the
    # probe would report a dead loop, and Portainer would grade the container unhealthy for
    # a fault the loop had nothing to do with. A database fault belongs in the log below,
    # not in the health status.
    write_heartbeat(heartbeat_path, logger)
    try:
        now_utc = datetime.now(timezone.utc)
        all_metrics = await db.get_all_metrics_with_reminder()
    except Exception:
        logger.exception("Error fetching metrics for reminders")
        return
    for metric in all_metrics:
        try:
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
            logger.exception("Failed reminder for metric {}", metric["id"])


class ReminderScheduler:
    def __init__(self, db: Database, bot: HealthBot):
        self.db = db
        self.bot = bot
        # Next to the database, i.e. on the volume. `db.db_path` is the value main.py built
        # this Database from — settings.database_path — and taking it from the object
        # instead of importing settings here keeps the mark provably beside the file the
        # application really opened. Database.__init__ has already created that directory,
        # so the first write below cannot fail for want of one.
        self.heartbeat_path = heartbeat_file(db.db_path)
        self.scheduler = AsyncIOScheduler()

    def start(self):
        self.scheduler.add_job(
            check_reminders,
            trigger="cron",
            minute="*",
            args=[self.db, self.bot, self.heartbeat_path],
        )
        self.scheduler.start()
        # One mark at startup, and it is not redundant with the job above. Docker schedules
        # the FIRST probe one --interval (30 s) after --start-period (30 s) elapses, while
        # the cron job does not fire until the next whole minute — so a container started at
        # HH:MM:31 would still have written nothing when the first probe arrived, which
        # would read a missing file and start burning retries on a perfectly healthy
        # container. This write makes the mark exist from the moment the loop is up.
        write_heartbeat(self.heartbeat_path, logger)
        logger.info("Scheduler started, heartbeat file {}", self.heartbeat_path)

    def stop(self):
        self.scheduler.shutdown(wait=False)
