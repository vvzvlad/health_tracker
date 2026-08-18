import asyncio

from loguru import logger

from src.settings import settings
from src.database import Database
from src.bot import HealthBot
from src.heartbeat import clear_heartbeat, heartbeat_file
from src.logging_setup import configure_logging
from src.scheduler import ReminderScheduler


async def main():
    # Before the first log line, because it is what decides where log lines go and what they
    # are allowed to contain: the redacting stderr sink, the bridge that routes aiogram's and
    # apscheduler's stdlib records into the same sink, and the excepthook.
    configure_logging(settings.telegram_bot_token, settings.log_level)
    logger.info("Starting Health Tracker Bot")
    db = Database(settings.database_path)
    # The heartbeat lives on the data volume, and the updater recreates this container over
    # the SAME volume — so the mark the outgoing container wrote seconds ago is on disk right
    # now. Deleting it here restores the meaning the probe reads it with: "there is no mark"
    # means THIS container has not ticked yet.
    # The position is the point. It is after Database(), which is what creates the directory
    # the mark lives in, and BEFORE db.init() and scheduler.start() — everything below this
    # line can block (db.init() on a database the outgoing container still holds, above all),
    # and a startup that hangs after this call leaves no mark at all, which is exactly the
    # verdict wanted. Doing it later would let a hung startup be graded on the previous
    # container's work.
    clear_heartbeat(heartbeat_file(db.db_path), logger)
    await db.init()
    bot = HealthBot(db)
    scheduler = ReminderScheduler(db, bot)
    scheduler.start()
    try:
        await bot.start()
    finally:
        scheduler.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
