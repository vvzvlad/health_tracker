import asyncio
import sys

from loguru import logger

from src.settings import settings
from src.database import Database
from src.bot import HealthBot
from src.scheduler import ReminderScheduler


async def main():
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.info("Starting Health Tracker Bot")
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
