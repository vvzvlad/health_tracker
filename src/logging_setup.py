"""Logging for the whole process: one sink, one format, and the token scrubbed on the way out.

A Telegram request URL carries the bot token in its path (`<server>/bot<token>/getUpdates`), so
any library that puts a request URL into an error message leaks the token; e.g. a schemeless
TELEGRAM_BOT_API_SERVER makes aiohttp raise NonHttpUrlClientError with the full URL. Three
measures:

* THE SINK REDACTS. Every line loguru writes (message, extra, traceback) is rendered and passed
  through redact() before reaching stderr. src/settings.py also rejects a schemeless server URL.
* STDLIB LOGGING IS REROUTED into loguru. aiogram, apscheduler and aiohttp log through stdlib
  `logging`; without a handler, records below WARNING are never created (root's default level)
  and those at WARNING+ go through `logging.lastResort`, bare, unattributed and unredacted. This
  is also the only place a permanent polling failure shows: aiogram's
  `Dispatcher._listen_updates` retries getUpdates forever, so a 409, a revoked token or a lost
  Bot API server after startup keeps the process and the healthcheck green. (Failures at startup
  kill the process, which the healthcheck catches.)
* sys.excepthook REDACTS TOO: the traceback of a fatal exception bypasses all handlers.

Not a guarantee: a subprocess writing to the inherited stderr, or C-level output, goes around
all three. What is scrubbed is everything this process logs and the traceback it dies of.
"""

import logging
import sys
import traceback

from loguru import logger

TOKEN_PLACEHOLDER = "***REDACTED***"
# The shortest string redact() will blind-replace, applied to the whole token and to its secret
# half. TELEGRAM_BOT_TOKEN has no format check, so a value like `test` is possible, and replacing
# every `test` would corrupt the log. A string this short cannot authenticate anything (a real
# token is `<numeric id>:<35-character secret>`).
MIN_SECRET_LENGTH = 8

# The level names loguru 0.7.2 accepts in `logger.add(level=...)` and `logger.level()`; anything
# else, including a correct name in the wrong case, raises ValueError. src/settings.py imports
# this to validate LOG_LEVEL, because configure_logging() runs before the sink and excepthook
# exist and a ValueError there would be an unformatted crash on every restart.
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
        # Start from THIS frame: `logging.currentframe` is `sys._getframe(3)` and would skip
        # frames that must be counted, attributing every line to logging/__init__.py.
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

    # `force=True` replaces any handlers already on the root logger.
    #
    # The root level is the real one, not NOTSET, so records below it are never built and
    # walked through InterceptHandler only to be dropped. loguru's level numbers are the
    # stdlib's, and logger.add() above has already rejected unknown names. A library logger
    # with its own lower level still gets through and is filtered by the sink.
    logging.basicConfig(
        handlers=[InterceptHandler()],
        level=logger.level(level).no if isinstance(level, str) else level,
        force=True,
    )

    def excepthook(exc_type, exc_value, exc_traceback):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        sys.stderr.write(redact(text, token))

    sys.excepthook = excepthook
