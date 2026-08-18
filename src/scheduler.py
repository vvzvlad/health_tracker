from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src.database import Database
from src.bot import HealthBot
from src.heartbeat import HEARTBEAT_INTERVAL, heartbeat_file, write_heartbeat
from src.utils import parse_timezone

# The reminder job's own misfire tolerance, spelled out because APScheduler's default is ONE
# SECOND: a tick the scheduler could not dispatch on time — the loop busy with another
# coroutine, a suspended container, a clock step — is dropped silently, and the reminders that
# minute is responsible for are simply not sent.
# What it buys and what it does not: the job is dispatched even when it is late, but
# check_reminders matches `remind_time` against the wall clock AT RUN TIME, so a tick that
# arrives in a later minute still will not send the earlier minute's reminders. The grace is
# therefore about the job running and the delay being visible in the log rather than about
# recovering a missed minute. It is on the reminder job only; the heartbeat job below wants
# the opposite (a late heartbeat is worth nothing, the next one is 30 s away).
REMINDER_MISFIRE_GRACE = 300


async def touch_heartbeat(heartbeat_path: str) -> None:
    """The whole body of the heartbeat job: one file write, on its own schedule.

    Separate from check_reminders BECAUSE that job does unbounded network I/O. APScheduler
    defaults to max_instances=1, so while one execution is still running the next tick is
    dropped — and check_reminders awaits `bot.send_reminder` per metric against an aiogram
    session whose per-request timeout is 60 s. A Bot API server that has stopped answering in
    the minute a few reminders are due is therefore minutes inside ONE execution, with every
    tick in between discarded. With the mark written by that job, the loop would go on
    turning, the mark would go stale, and the probe would report `unhealthy` on a service
    whose only fault was a slow upstream — handing the updater a rollback of a perfectly good
    image. A probe that is wrong in THAT direction is worse than no probe at all.

    So this job touches nothing that can block: no database, no network, one open() and one
    write() of a handful of bytes. The heartbeat then means what the healthcheck reads it as —
    the asyncio loop is still dispatching — and nothing else.
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
        # A fixed path in the container's own writable layer, NOT next to the database and not
        # derived from it — src/heartbeat.py explains at length why the data volume is the one
        # place this must never be. Nothing about the Database is consulted here, which is what
        # makes that independence hold even if DATABASE_PATH moves.
        self.heartbeat_path = heartbeat_file()
        self.scheduler = AsyncIOScheduler()

    def start(self):
        self.scheduler.add_job(
            check_reminders,
            trigger="cron",
            minute="*",
            id="check_reminders",
            args=[self.db, self.bot],
            # All three spelled out rather than left to the defaults, because the defaults are
            # what made the heartbeat unreliable while it lived in this job: misfire_grace_time
            # is 1 s (see REMINDER_MISFIRE_GRACE above), max_instances is 1 and coalesce is
            # True. The last two are KEPT at their defaults on purpose — two concurrent reminder
            # sweeps would race on `last_reminded_date` and could send a metric twice, and a
            # backlog of coalesced ticks has nothing to catch up on.
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
            # No misfire grace of its own: a heartbeat that arrives late says the loop was
            # blocked, which is the very thing the probe is supposed to notice, and the next
            # tick is only 30 s away. max_instances=1 and coalesce because a queue of pending
            # heartbeat writes would be a queue of identical one-line writes.
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        # One mark at startup, and it is not redundant with the job above. With a
        # --start-period set, docker probes every --start-interval (5 s by default) from the
        # moment the container starts, while an interval job's FIRST run is one whole interval
        # (30 s) after the scheduler starts — so without this write the first several probes
        # would read a missing file. They would not cost the container anything (failures
        # inside the start period do not count towards --retries), but they would delay
        # `healthy` by 30 s inside a ~120 s updater window. This write makes the mark exist
        # from the moment the loop is up.
        write_heartbeat(self.heartbeat_path, logger)
        logger.info("Scheduler started, heartbeat file {}", self.heartbeat_path)

    def stop(self):
        self.scheduler.shutdown(wait=False)
