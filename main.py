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
    # A no-op on the default path, and deliberately so: the mark lives in the container's own
    # writable layer, which is empty on a fresh container, so there is normally nothing here to
    # delete. It is the HEARTBEAT_FILE override that this covers — pointed at a volume or a bind
    # mount, the mark CAN be inherited from the container this one replaced, and a fresh
    # container would be graded on the outgoing one's work. See src/heartbeat.py.
    # The position is the point: BEFORE db.init() and scheduler.start(). Everything below this
    # line can block (db.init() on a database the outgoing container still holds, above all),
    # and a startup that hangs after this call leaves no mark at all, which is exactly the
    # verdict wanted.
    clear_heartbeat(heartbeat_file(), logger)
    db = Database(settings.database_path)
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
