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
  stdlib `logging` module, and main.py used to configure loguru alone — so those records had
  no handler at all and went out through `logging.lastResort`: stderr, WARNING and above only,
  a different format, and past both LOG_LEVEL and the redaction above. Routing them here is
  also the only reason a polling failure is visible at all: aiogram's
  `Dispatcher._listen_updates` catches every exception around getUpdates and retries forever,
  so a bot that can reach nothing keeps its process, its scheduler and its heartbeat, and the
  healthcheck stays green. The log is where that failure appears, or nowhere.
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
# A bot token is `<numeric id>:<35-character secret>`. Both halves are replaced, because a
# library that splits the URL may print the secret on its own — but only when the secret is
# long enough to be unambiguous. Blind-replacing a three-character string inside a log line
# would corrupt unrelated text, and a token that short is not a real one anyway.
MIN_SECRET_LENGTH = 8


def redact(text, token):
    """Replace the token, and its secret half alone, with a placeholder.

    Returns `text` unchanged when there is nothing to scrub, so callers do not have to care
    whether a token was configured.
    """
    if not text or not token:
        return text
    result = text.replace(token, TOKEN_PLACEHOLDER)
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
    """Install the redacting sink, the stdlib bridge and the excepthook. Call once, first."""
    logger.remove()

    def sink(message):
        # `message` is loguru's fully rendered line, trailing newline and formatted traceback
        # included, which is exactly what has to be scrubbed — redacting record["message"]
        # alone would leave the traceback untouched, and the traceback is where the URL is.
        sys.stderr.write(redact(str(message), token))

    logger.add(sink, level=level)

    # `force=True` replaces whatever handlers are already on the root logger, so this is not
    # additive with a library that configured logging at import time. Level NOTSET lets every
    # record reach the handler and leaves the filtering to loguru's own sink level, which is
    # what LOG_LEVEL is supposed to control.
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.NOTSET, force=True)

    def excepthook(exc_type, exc_value, exc_traceback):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        sys.stderr.write(redact(text, token))

    sys.excepthook = excepthook
