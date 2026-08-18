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

    Both hazards are what a plain sequence of statements in a `finally` walks into. An exception
    from one step skips every step below it — and the step that must never be skipped is
    `db.close()`, because an unclosed aiosqlite connection is precisely what keeps the interpreter
    alive (see the `finally` in main()). And an exception raised inside a `finally` REPLACES the
    exception already on its way out, so a shutdown that broke while cleaning up after a failure
    would report itself instead of the failure — including HeartbeatUnwritable, whose entire value
    is the explanatory line it puts in the log.

    A coroutine step and a plain one both go through here (`db.close` is awaited, `scheduler.stop`
    is not), so the caller reads as one list of steps rather than as three shapes of try/except.

    Failures are logged rather than swallowed silently: by this point configure_logging() has run,
    so the line goes through the redacting sink like everything else.
    """
    try:
        result = step()
        if inspect.isawaitable(result):
            await result
    except Exception as error:  # noqa: BLE001 - a broken cleanup step must not mask the real one
        logger.warning("Failed to {} while shutting down: {}", what, error)


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
    # Constructing this OPENS NOTHING — it stores the path and creates the parent directory — so a
    # raise here (a read-only mount, say) is a clean death: no connection, no thread, nothing for
    # the `finally` to close. That is why this one line is allowed to stay outside the try, and it
    # is the only one.
    db = Database(settings.database_path)
    # Bound before the try so the `finally` can ask whether each was ever built: the try is entered
    # with both still None and any of the lines that create them can raise.
    bot = None
    scheduler = None
    # EVERYTHING THAT CAN FAIL ONCE A DATABASE OBJECT EXISTS IS INSIDE THIS try — db.init() and both
    # constructors included — and the `finally` is not housekeeping: without it the process HANGS
    # instead of dying. aiosqlite runs its connection on a NON-daemon thread (aiosqlite 0.20,
    # `class Connection(Thread)`, no daemon flag), so an unclosed database keeps the interpreter
    # alive after the traceback has been printed: measured, a container whose first Bot API call
    # failed printed its traceback and then sat there for 200+ s, doing nothing, never exiting.
    #
    # THE TWO CONSTRUCTORS ARE IN HERE FOR A REASON THAT COSTS A CONTAINER, not for symmetry. They
    # used to sit between db.init() and the try, under a comment claiming everything fallible was
    # already inside it. `HealthBot.__init__` builds `Bot(token=...)`, and aiogram calls
    # `validate_token` as its first statement: an EMPTY token, a token with no colon and a token
    # with a space in it each raise TokenValidationError right there. `TELEGRAM_BOT_TOKEN=`
    # declared in a compose file and left blank is the ordinary way to produce one —
    # `telegram_bot_token: str` accepts the empty string, pydantic-settings does not read a blank
    # variable as absent — and with the connection already open and the raise outside the try, that
    # printed a traceback and then hung exactly as above (reproduced: still alive 25 s later on
    # python 3.11.8 with the pinned requirements, and inside the image on the CI daemon).
    # db.init() is in here for the same reason and covers more: `PRAGMA journal_mode=WAL`, the
    # ALTER and the records rebuild all run on a connection that is already open, so a corrupt
    # database file, a read-only volume or a full disk left the same hung process behind. aiosqlite
    # cleans up after itself only when `connect()` ITSELF fails.
    # `healthy` never arrives either way, so the updater rolls the image back regardless — but a
    # container that hangs is not a container that died, and `restart: unless-stopped` cannot even
    # restart it.
    try:
        await db.init()
        bot = HealthBot(db)
        scheduler = ReminderScheduler(db, bot)
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
        # THE DATABASE GOES FIRST, and the order is load-bearing rather than tidy. It is the one
        # close that decides whether this process can exit at all, and the two steps that used to
        # stand above it can both raise: aiogram's session close does I/O, and ReminderScheduler.stop()
        # only guards the one failure somebody predicted (a scheduler that never started) —
        # APScheduler's shutdown() can still fail on anything else. Either of them raising would
        # have skipped db.close(), putting the hang back, AND replaced the exception on its way out
        # — including the HeartbeatUnwritable line, which exists to be read.
        # Each step is isolated for the same reason; shutdown_step() explains it.
        # The one cost of closing the database first: a reminder tick that fires in the gap before
        # the scheduler stops finds a closed connection. check_reminders() catches that around its
        # own query and logs it, which is a stray line in the log of a process that is dying
        # anyway — cheap against a container that never exits.
        await shutdown_step("close the database", db.close)
        if scheduler is not None:
            await shutdown_step("stop the scheduler", scheduler.stop)
        if bot is not None:
            await shutdown_step("close the bot session", bot.close)


if __name__ == "__main__":
    asyncio.run(main())
