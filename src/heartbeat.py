"""The heartbeat file: written by the heartbeat job, read by the Docker HEALTHCHECK probe.

This module is the single place the heartbeat contract is defined, and it is deliberately
STDLIB-ONLY — it imports neither pydantic nor `src.settings`.

Why that matters: `src/healthcheck.py` (the probe docker runs) imports this module. If the
probe reached `src.settings` instead, building `Settings` would raise a ValidationError
whenever TELEGRAM_BOT_TOKEN was absent — so the probe would report "unhealthy" for a
CONFIGURATION reason and mask the liveness verdict it exists to give. Keeping the defaults
here lets `src/settings.py` and the probe share one definition without the probe inheriting
the app's configuration requirements.

WHAT THE FILE CONTAINS, and why it is not just a timestamp: the mark carries the PID of the
process that wrote it, ahead of the time it was written. The mark lives on the data volume,
and the updater REPLACES the container over the SAME volume — so at the moment a new
container starts, a mark left by the container being retired is sitting there, seconds old.
A probe that only looked at the age would read that inherited mark and report `healthy`
before the new container had written anything at all, which is exactly the case this
healthcheck exists to catch: a process that came up and wedged before its scheduler ever
started — `db.init()` blocking on a database the outgoing container still holds, say.
Two things close that hole, and they are deliberately independent: main.py DELETES the mark
at startup (before anything that can block), and the PID in the file means an inherited mark
that somehow survived the deletion still names a process that is not this container's PID 1.
"""

import os
import time

# The default database path. It lives HERE rather than as a literal in src/settings.py
# because the probe has to derive the heartbeat path from the same value without importing
# Settings (see the module docstring); src/settings.py imports it back as the field default,
# so there is still exactly one definition of it.
DEFAULT_DATABASE_PATH = "data/health.db"

# Fixed name, written next to the database — i.e. on the volume production mounts at the data
# directory. Deliberately not /tmp: the mark then sits where the data does, survives with it,
# and can be read by anybody who looks at the volume to ask when the bot last ticked.
HEARTBEAT_FILENAME = "heartbeat"

# How often src/scheduler.py rewrites the mark. It is a job of its own — one write, no network
# and no database — precisely so that the number below can be trusted; see the comment on that
# job for what sharing the reminder job's schedule used to cost.
HEARTBEAT_INTERVAL = 30

# 90 s = three missed ticks of the 30 s heartbeat job. One missed tick proves nothing (a slow
# await, a clock adjustment, a job that fired a second late) and would make the probe flap;
# three in a row is a loop that has stopped turning. The number IS three of that job's
# intervals, so it moves only if HEARTBEAT_INTERVAL moves.
# It is also bounded from ABOVE, and that bound is why it is 90 and not 180: the updater waits
# roughly 120 s for a freshly deployed container to report `healthy`, so a tolerance wider than
# that window would let one mark carry a container through the whole of it — the container
# would be graded on a single write and never on whether the loop kept turning.
HEARTBEAT_MAX_AGE = 90

# The PID the mark has to carry to count. The image's CMD is in exec form
# (`CMD ["python", "main.py"]`), so the interpreter running the bot IS the container's PID 1 —
# there is no shell in between to take that number. Anything else in the file is a mark this
# container did not write, and the only way one gets there is the shared volume described in
# the module docstring.
CONTAINER_MAIN_PID = 1


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


def format_mark(pid, timestamp):
    """The ONE definition of what goes in the file: the writer's PID, then the unix time.

    Exposed rather than inlined into write_heartbeat() because the CI smoke gate stages marks
    of its own — one aged past the limit, one bearing a foreign PID — and a second spelling of
    the format over there would let the two drift apart while both went on looking right.
    """
    return "{} {}".format(int(pid), int(timestamp))


def write_heartbeat(path, logger=None):
    """Best-effort liveness mark; heartbeat I/O must never break the job writing it.

    A raise here would propagate out of the job and be swallowed by APScheduler, which would
    cost the heartbeat over a full disk without saying so. A warning in the log and a stale
    mark say the same thing far more usefully: the probe will report it.
    """
    try:
        with open(path, "w") as handle:
            handle.write(format_mark(os.getpid(), time.time()))
    except Exception as error:  # noqa: BLE001 - a failed heartbeat must not raise
        if logger is not None:
            logger.warning("Failed to write heartbeat {}: {}", path, error)


def clear_heartbeat(path, logger=None):
    """Drop whatever mark is already on the volume, at the very start of a process.

    This is the half of the inherited-mark defence that does not depend on the PID check: it
    restores the meaning of "there is no mark" to "THIS container has not ticked yet", which
    is what the probe's missing-file branch is written against. It has to run before anything
    that can block — the failure it exists for is a startup that hangs, and a startup that
    hangs after this call is a container with no mark at all, which is precisely the verdict
    wanted.

    Best-effort, for the same reason write_heartbeat() is: a read-only or full volume is a
    problem to report, not a reason to refuse to start. The PID in the mark covers the case
    where the removal did not work.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        if logger is not None:
            logger.warning("Failed to remove inherited heartbeat {}: {}", path, error)


def read_heartbeat(path):
    """Return (pid, written_at) from the mark.

    Raises OSError when the file is missing or unreadable, and ValueError when it holds
    something other than the two integers format_mark() writes. The caller has to keep those
    apart: a missing mark is a container that has not ticked yet, a malformed one is a writer
    that is broken — and both are "not healthy", but only one of them is normal in the first
    seconds of a container's life.
    """
    with open(path) as handle:
        content = handle.read().strip()
    parts = content.split()
    if len(parts) != 2:
        raise ValueError("expected '<pid> <unix time>', found {!r}".format(content[:80]))
    return int(parts[0]), int(parts[1])
