"""Logging for the whole process: one sink, one format, and the token scrubbed on the way out.

Three separate things are set up here and they all answer the same failure. A Telegram request
URL carries the bot token in its PATH — `<server>/bot<token>/getUpdates` — so any library that
puts a request URL into an error message puts the token into it as well. That is not
hypothetical: give TELEGRAM_BOT_API_SERVER a value with the scheme left off (`10.0.0.1:8081`
instead of `http://10.0.0.1:8081`, the obvious typo) and aiohttp raises NonHttpUrlClientError
whose text is the full URL, token included; aiogram wraps it in TelegramNetworkError and it
goes out as a log line or a traceback. `docker logs` then keeps it for as long as the
container lives, and the updater's containers live a long time.

* THE SINK REDACTS. Every line loguru writes — message, extra, and the traceback of an
  exception logged with `logger.exception` — is rendered first and passed through redact()
  before it reaches stderr. src/settings.py refuses a schemeless server URL outright, so the
  path above is closed at its source; this covers the paths nobody has thought of yet.
* STDLIB LOGGING IS REROUTED into loguru. aiogram, apscheduler and aiohttp all log through the
  stdlib `logging` module, and main.py used to configure loguru alone — so those records had no
  handler anywhere on their chain. Two things then happened to them, and they are worth keeping
  apart because the usual one-line summary ("lastResort drops them") names the wrong mechanism.
  A record below WARNING never got as far as a handler at all: `Logger.isEnabledFor` compares
  against the EFFECTIVE level, which with nothing configured is the root logger's default of
  WARNING, so the record was not even created — verified on this project's interpreter. A record
  at WARNING or above WAS emitted, through `logging.lastResort`: stderr, bare message, no
  attribution, and past both LOG_LEVEL and the redaction above. Routing them here is
  also the only reason a polling failure is visible at all: aiogram's
  `Dispatcher._listen_updates` catches every exception around getUpdates and retries forever
  behind a backoff, so a bot whose polling has permanently failed — a 409 because something
  else started polling the same token, a revoked token, a Bot API server that went away after
  startup — keeps its process, its scheduler and its heartbeat, and the healthcheck stays
  green. The log is where that failure appears, or nowhere. (A failure already present at
  STARTUP does not get that far: `set_my_commands` raises before polling begins and the process
  dies, which the healthcheck does catch.)
* sys.excepthook REDACTS TOO. The traceback of an exception that kills the process is printed
  by the interpreter itself and never passes through a handler of any kind.

What this module deliberately does NOT do is guarantee the token cannot leak. A subprocess
that writes to the inherited stderr, or C-level output, goes around all three. The guarantee
is narrower and worth stating exactly: everything THIS process logs, and the traceback it dies
of, are scrubbed.
"""

import logging
import sys
import traceback

from loguru import logger

TOKEN_PLACEHOLDER = "***REDACTED***"
# The shortest string this will blind-replace throughout a log line. It applies to BOTH the
# whole token and its secret half, and the bound on the whole token is the one that matters:
# TELEGRAM_BOT_TOKEN is a plain `str` with no format check, so `TELEGRAM_BOT_TOKEN=test` is a
# thing a stack can really be given — and replacing every occurrence of `test` in everything
# this process logs would corrupt arbitrary text, starting with the traceback that says the
# token is invalid. Below the bound the choice is deliberate: a string that short cannot
# authenticate anything (a real token is `<numeric id>:<35-character secret>`), so keeping the
# log readable is worth more than scrubbing it. A real token, and a real token's secret half,
# are both far above this.
MIN_SECRET_LENGTH = 8

# The level names loguru knows, and the ONLY values `logger.add(level=...)` and `logger.level()`
# accept. Checked against loguru 0.7.2, which is what requirements.txt pins: both raise
# `ValueError: Level 'info' does not exist` for anything outside this tuple — INCLUDING a correct
# name in the wrong case, which is the trap this exists for.
#
# It lives here, next to the code that consumes it, and src/settings.py imports it to validate
# LOG_LEVEL. That is the position that matters: configure_logging() runs BEFORE the sink and the
# excepthook exist, so a ValueError raised inside it is an unformatted crash on every restart,
# forever — the same class of failure as the empty TELEGRAM_BOT_API_SERVER. Rejecting the value
# in Settings instead at least names the field and the accepted spellings.
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")


def redact(text, token):
    """Replace the token, and its secret half alone, with a placeholder.

    Both halves, because a library that splits the URL may print the secret on its own.

    Returns `text` unchanged when there is nothing to scrub, so callers do not have to care
    whether a token was configured.
    """
    if not text or not token:
        return text
    result = text
    if len(token) >= MIN_SECRET_LENGTH:
        result = result.replace(token, TOKEN_PLACEHOLDER)
    secret = token.partition(":")[2]
    if len(secret) >= MIN_SECRET_LENGTH:
        result = result.replace(secret, TOKEN_PLACEHOLDER)
    return result


class InterceptHandler(logging.Handler):
    """Hand every stdlib record to loguru, so there is exactly one sink in the process.

    The frame walk is what keeps the source location honest: without it every intercepted line
    would be attributed to this file instead of to the aiogram or apscheduler module that
    really emitted it.
    """

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            # A custom numeric level loguru has no name for. Pass the number through rather
            # than dropping the record.
            level = record.levelno
        # `sys._getframe()` is THIS frame, not logging's idea of the caller — `logging.currentframe`
        # is `sys._getframe(3)` and starting there lands past the frames that have to be counted,
        # which shows every intercepted line as coming from logging/__init__.py.
        frame, depth = sys._getframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        # `record.getMessage()` has already had its %-args applied. It is passed with no
        # further arguments on purpose: loguru only treats a message as a format template when
        # arguments accompany it, so a stdlib message containing braces stays literal.
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(token, level="INFO"):
    """Install the redacting sink, the stdlib bridge and the excepthook. Call once, first.

    `level` must already be one of LOG_LEVELS. This function CANNOT report a bad one usefully —
    it runs before the sink and the excepthook it is installing exist, so a raise here is an
    unformatted traceback in a restart loop. src/settings.py is where a value out of the
    environment is normalised and rejected, and it names the variable when it does.
    """
    logger.remove()

    def sink(message):
        # `message` is loguru's fully rendered line, trailing newline and formatted traceback
        # included, which is exactly what has to be scrubbed — redacting record["message"]
        # alone would leave the traceback untouched, and the traceback is where the URL is.
        sys.stderr.write(redact(str(message), token))

    logger.add(sink, level=level)

    # `force=True` replaces whatever handlers are already on the root logger, so this is not
    # additive with a library that configured logging at import time.
    #
    # THE LEVEL IS THE REAL ONE, not NOTSET. At NOTSET every DEBUG record any library emits is
    # built, %-formatted, handed to InterceptHandler and walked frame by frame — all to be
    # dropped by the sink at the end of it. Setting the root threshold means those records are
    # never created. loguru's level numbers ARE the stdlib's (DEBUG 10, INFO 20, …), so its own
    # table is what translates LOG_LEVEL here; it cannot fail, because logger.add() above has
    # already rejected any name loguru does not know.
    # A library that sets its OWN logger level lower still gets through — a logger with an
    # explicit level ignores the root's — and loguru's sink filters it, exactly as before.
    logging.basicConfig(
        handlers=[InterceptHandler()],
        level=logger.level(level).no if isinstance(level, str) else level,
        force=True,
    )

    def excepthook(exc_type, exc_value, exc_traceback):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        sys.stderr.write(redact(text, token))

    sys.excepthook = excepthook
