import asyncio

from loguru import logger

from src.settings import settings
from src.database import Database
from src.bot import HealthBot
from src.heartbeat import HeartbeatUnwritable, clear_heartbeat, heartbeat_file
from src.logging_setup import configure_logging
from src.scheduler import ReminderScheduler


async def main():
    # Before the first log line, because it is what decides where log lines go and what they
    # are allowed to contain: the redacting stderr sink, the bridge that routes aiogram's and
    # apscheduler's stdlib records into the same sink, and the excepthook.
    configure_logging(settings.telegram_bot_token, settings.log_level)
    logger.info("Starting Health Tracker Bot")
    # NOT a no-op, whatever the "the writable layer is created empty" story suggests. That story
    # is about the container being RECREATED — an updater replacing it — and it is true. But the
    # layer, and the mark in it, SURVIVE A RESTART of the same container: `docker restart`,
    # `restart: unless-stopped` after the process falls over, a host reboot. The new run then
    # finds the previous run's mark, at most 30 s old, while docker has reset the health state to
    # `starting` and probes from scratch — so the first probe would report healthy a process
    # still sitting in db.init(). This call is what makes that impossible. It covers a
    # HEARTBEAT_FILE pointed at a volume as well. See src/heartbeat.py.
    # The position is the point: BEFORE db.init() and scheduler.start(). Everything below this
    # line can block (db.init() on a database the outgoing container still holds, above all),
    # and a startup that hangs after this call leaves no mark at all, which is exactly the
    # verdict wanted.
    clear_heartbeat(heartbeat_file(), logger)
    db = Database(settings.database_path)
    await db.init()
    bot = HealthBot(db)
    scheduler = ReminderScheduler(db, bot)
    # EVERYTHING THAT CAN FAIL AFTER db.init() IS INSIDE THIS try, and the `finally` is not
    # housekeeping — without it the process HANGS instead of dying. aiosqlite runs its connection
    # on a NON-daemon thread (aiosqlite 0.20, `class Connection(Thread)`, no daemon flag), so an
    # unclosed database keeps the interpreter alive after the traceback has been printed: measured,
    # a container whose first Bot API call failed printed its traceback and then sat there for
    # 200+ s, doing nothing, never exiting. The first draft of the reordering below had exactly
    # that bug, because contact_api() sat above the try. `healthy` never arrives either way, so the
    # updater still rolls back — but a container that hangs is not a container that died, and
    # `restart: unless-stopped` cannot even restart it.
    try:
        # BEFORE the scheduler, and that ordering is the whole of what makes `healthy` mean
        # anything about the bot rather than about the process. The scheduler's start() writes the
        # first liveness mark; this line is the first Bot API request. Below this order, a container
        # whose Bot API address merely BLACK-HOLES traffic had its mark on disk a second or two in,
        # was healthy from its first probe, and only died 60 s later on aiogram's request timeout —
        # right through the updater's window, forever, on a restart policy. See
        # HealthBot.contact_api for what the delay costs.
        await bot.contact_api()
        scheduler.start()
        await bot.start()
    except HeartbeatUnwritable as error:
        # The one heartbeat failure that is fatal. Reported HERE rather than left to the
        # excepthook so that it arrives as one readable line through the redacting sink, which is
        # what `docker logs` shows; `from None` keeps the traceback of an OSError about a file
        # permission out of a message that already says everything useful.
        logger.error("{}", error)
        raise SystemExit(1) from None
    finally:
        scheduler.stop()
        await bot.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
