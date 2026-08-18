"""The heartbeat file: written by the reminder job, read by the Docker HEALTHCHECK probe.

This module is the single place the heartbeat contract is defined, and it is deliberately
STDLIB-ONLY — it imports neither pydantic nor `src.settings`.

Why that matters: `src/healthcheck.py` (the probe docker runs) imports this module. If the
probe reached `src.settings` instead, building `Settings` would raise a ValidationError
whenever TELEGRAM_BOT_TOKEN was absent — so the probe would report "unhealthy" for a
CONFIGURATION reason and mask the liveness verdict it exists to give. Keeping the defaults
here lets `src/settings.py` and the probe share one definition without the probe inheriting
the app's configuration requirements.
"""

import os
import time

# The default database path. It lives HERE rather than as a literal in src/settings.py
# because the probe has to derive the heartbeat path from the same value without importing
# Settings (see the module docstring); src/settings.py imports it back as the field default,
# so there is still exactly one definition of it.
DEFAULT_DATABASE_PATH = "data/health.db"

# Fixed name, written next to the database — i.e. on the volume that production mounts
# (telegram_bots_health_tracker -> /app/data). Deliberately not /tmp: the mark then sits
# where the data does, survives with it, and can be read by anybody who looks at the volume
# to ask when the bot last ticked.
HEARTBEAT_FILENAME = "heartbeat"

# 180 s = three missed ticks of the once-a-minute cron job in src/scheduler.py that writes
# the mark. One missed tick proves nothing (a slow await, a clock adjustment, a job that
# fired a second late) and would make the probe flap; three in a row is a loop that has
# stopped turning. The number IS three of that job's intervals, so it moves only if the
# job's `minute="*"` trigger moves.
HEARTBEAT_MAX_AGE = 180


def heartbeat_file(database_path=None):
    """Resolve where the mark lives.

    HEARTBEAT_FILE wins outright; that override exists so the probe can be pointed at a
    staged file and tested (the CI smoke gate does exactly that). Otherwise the mark sits
    next to the database.

    The application passes `database_path` — the path it really opened. The probe passes
    nothing and the environment answers instead: DATABASE_PATH is the variable
    pydantic-settings reads into `Settings.database_path`, so both sides land on the same
    file. The one place the two could disagree is a DATABASE_PATH set ONLY in a local
    `.env`, which `Settings` reads and this function does not — in the container that
    cannot happen, because the image copies just src/ and main.py and the stack passes its
    configuration as real environment variables.
    """
    override = os.getenv("HEARTBEAT_FILE")
    if override:
        return override
    if database_path is None:
        database_path = os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH)
    # `or "."` covers a bare filename like DATABASE_PATH=health.db, whose dirname is "".
    return os.path.join(os.path.dirname(database_path) or ".", HEARTBEAT_FILENAME)


def write_heartbeat(path, logger=None):
    """Best-effort liveness mark; heartbeat I/O must never break the job writing it.

    A raise here would propagate out of the cron job and be swallowed by APScheduler,
    which would cost a reminder round over a full disk. A warning in the log and a stale
    mark say the same thing far more usefully: the probe will report it.
    """
    try:
        with open(path, "w") as handle:
            handle.write(str(int(time.time())))
    except Exception as error:  # noqa: BLE001 - a failed heartbeat must not raise
        if logger is not None:
            logger.warning("Failed to write heartbeat {}: {}", path, error)


def heartbeat_age(path):
    """Seconds since `path` was last written. Raises OSError when it is missing."""
    return time.time() - os.path.getmtime(path)
