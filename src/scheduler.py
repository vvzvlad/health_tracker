from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src.database import Database
from src.bot import HealthBot
from src.heartbeat import (
    HEARTBEAT_INTERVAL, heartbeat_file, write_first_heartbeat, write_heartbeat)
from src.utils import parse_timezone

# The reminder job's misfire tolerance. APScheduler's default is 1 s, so a late tick (busy
# loop, suspended container, clock step) would be dropped silently. check_reminders matches
# `remind_time` against the wall clock at run time, so this does not recover a missed minute;
# it only lets the job run and the delay show in the log. The heartbeat job has no grace.
REMINDER_MISFIRE_GRACE = 300


async def touch_heartbeat(heartbeat_path: str) -> None:
    """The whole body of the heartbeat job: one file write, on its own schedule.

    Separate from check_reminders because that job does unbounded network I/O: with
    max_instances=1, one execution blocked on a slow Bot API (60 s per request, per metric) drops
    every tick until it ends, so a mark written there would go stale and the probe would get a
    good image rolled back over a slow upstream. This job touches no database and no network,
    so the mark means exactly "the asyncio loop is dispatching".
    """
    write_heartbeat(heartbeat_path, logger)


async def check_reminders(db: Database, bot: HealthBot) -> None:
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
        # A fixed path in the container's own writable layer, not derived from the database
        # path; src/heartbeat.py explains why the data volume must never hold it.
        self.heartbeat_path = heartbeat_file()
        self.scheduler = AsyncIOScheduler()

    def start(self):
        self.scheduler.add_job(
            check_reminders,
            trigger="cron",
            minute="*",
            id="check_reminders",
            args=[self.db, self.bot],
            # Spelled out: misfire_grace_time defaults to 1 s (see REMINDER_MISFIRE_GRACE).
            # max_instances=1 and coalesce match the defaults on purpose: concurrent sweeps would
            # race on `last_reminded_date` and could send a metric twice, and coalesced ticks
            # have nothing to catch up on.
            misfire_grace_time=REMINDER_MISFIRE_GRACE,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            touch_heartbeat,
            trigger="interval",
            seconds=HEARTBEAT_INTERVAL,
            id="heartbeat",
            args=[self.heartbeat_path],
            # No misfire grace: a late heartbeat means the loop was blocked, which is what the
            # probe should notice. A queue of pending heartbeat writes would be pointless.
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        # One mark at startup: an interval job first runs one interval (30 s) after start, past
        # every 5 s start-period probe, so without this `healthy` would arrive at ~60 s instead of
        # ~5 s (see the HEALTHCHECK comment in the Dockerfile).
        #
        # Unlike the periodic writes this one raises (see write_first_heartbeat()): a mark that
        # never appears gets a working bot rolled back on every deploy. main.py turns the
        # exception into a last log line and a stopped container.
        #
        # By now main.py has already reached the Bot API once (HealthBot.contact_api).
        write_first_heartbeat(self.heartbeat_path)
        logger.info("Scheduler started, heartbeat file {}", self.heartbeat_path)

    def stop(self):
        """Tolerates never having been started.

        main.py's `finally` also runs when startup failed before or during start(), and
        APScheduler's shutdown() raises SchedulerNotRunningError on a scheduler that is not
        running, which would replace the real failure.
        """
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
