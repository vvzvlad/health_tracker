import asyncio
import inspect

from loguru import logger

from src.settings import settings
from src.database import Database
from src.bot import HealthBot
from src.heartbeat import HeartbeatUnwritable, clear_heartbeat, heartbeat_file
from src.logging_setup import configure_logging
from src.scheduler import ReminderScheduler


async def shutdown_step(what, step):
    """Run ONE cleanup step, isolated, so that its failure can neither skip the others nor hide
    the failure that started the shutdown.

    In a plain `finally`, a raising step skips every later step (and `db.close()` must never be
    skipped: an unclosed aiosqlite connection keeps the interpreter alive), and an exception
    raised in a `finally` replaces the one already propagating, e.g. HeartbeatUnwritable.

    Accepts both coroutine steps (`db.close`) and plain ones (`scheduler.stop`). Failures are
    logged through the redacting sink.
    """
    try:
        result = step()
        if inspect.isawaitable(result):
            await result
    except Exception as error:  # noqa: BLE001 - a broken cleanup step must not mask the real one
        logger.warning("Failed to {} while shutting down: {}", what, error)


async def main():
    # First, because it decides where log lines go and what they may contain: the redacting
    # stderr sink, the stdlib-to-loguru bridge, and the excepthook.
    configure_logging(settings.telegram_bot_token, settings.log_level)
    logger.info("Starting Health Tracker Bot")
    # Not a no-op: the writable layer, and the mark in it, survive a restart of the same container
    # (`docker restart`, a restart policy, a host reboot), and the first probe would grade the
    # previous run's mark. Also covers a HEARTBEAT_FILE on a volume. See src/heartbeat.py.
    # Must run before db.init() and scheduler.start(), which can block; a startup that hangs
    # after this leaves no mark at all.
    clear_heartbeat(heartbeat_file(), logger)
    # Constructing this opens nothing (it stores the path and creates the parent directory), so a
    # raise here leaves nothing for the `finally` to close; it is the only line outside the try.
    db = Database(settings.database_path)
    # Bound before the try so the `finally` can tell whether each was built.
    bot = None
    scheduler = None
    # Everything that can fail once a Database object exists is inside this try, and the
    # `finally` is what lets the process die instead of hang: aiosqlite (0.20) runs its connection
    # on a non-daemon thread, so an unclosed database keeps the interpreter alive after the
    # traceback (measured: 200+ s, never exiting), and `restart: unless-stopped` cannot restart a
    # hung container. This includes:
    #   * the constructors: `Bot(token=...)` raises TokenValidationError for an empty, colon-less
    #     or space-containing token (e.g. a blank `TELEGRAM_BOT_TOKEN=` in compose);
    #   * db.init(): WAL pragma, ALTER and records rebuild run on an open connection, so a corrupt
    #     file, read-only volume or full disk fail there. aiosqlite cleans up only when
    #     `connect()` itself fails.
    try:
        await db.init()
        bot = HealthBot(db)
        scheduler = ReminderScheduler(db, bot)
        # BEFORE the scheduler: scheduler.start() writes the first liveness mark, so `healthy`
        # implies the Bot API answered. Otherwise a black-holed Bot API address would be healthy
        # for the 60 s until aiogram's timeout, through the updater's window. See
        # HealthBot.contact_api.
        await bot.contact_api()
        scheduler.start()
        await bot.start()
    except HeartbeatUnwritable as error:
        # The one fatal heartbeat failure, logged as one readable line through the redacting
        # sink; `from None` drops the OSError traceback, which adds nothing.
        logger.error("{}", error)
        raise SystemExit(1) from None
    finally:
        # The database closes first: an unclosed aiosqlite connection keeps the process alive,
        # and the steps below can raise (aiogram's session close does I/O, APScheduler's
        # shutdown() can fail). Each step is isolated by shutdown_step(). While the database is
        # already closed both jobs can still fire: a reminder tick logs a failed query, a
        # heartbeat tick may write a mark that the next start clears.
        await shutdown_step("close the database", db.close)
        if scheduler is not None:
            await shutdown_step("stop the scheduler", scheduler.stop)
        if bot is not None:
            await shutdown_step("close the bot session", bot.close)


if __name__ == "__main__":
    asyncio.run(main())
