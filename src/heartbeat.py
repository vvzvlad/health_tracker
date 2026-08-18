"""The heartbeat file: written by the heartbeat job, read by the Docker HEALTHCHECK probe.

This module is the single place the heartbeat contract is defined, and it is deliberately
STDLIB-ONLY — it imports neither pydantic nor `src.settings`.

Why that matters: `src/healthcheck.py` (the probe docker runs) imports this module. If the
probe reached `src.settings` instead, building `Settings` would raise a ValidationError
whenever TELEGRAM_BOT_TOKEN was absent — so the probe would report "unhealthy" for a
CONFIGURATION reason and mask the liveness verdict it exists to give.

WHAT THE FILE CONTAINS: one number, the unix time it was written at. Nothing identifies the
writer, because nothing has to — see DEFAULT_HEARTBEAT_FILE below for where the mark lives and
why that alone settles who wrote it.
"""

import os
import time

# WHERE THE MARK LIVES, and this is the whole of the inherited-mark defence.
#
# NOT on the data volume, and that is the mechanism rather than a preference. Portainer's
# updater REPLACES the container over the SAME data volume, so a mark written on that volume by
# the container being retired is on disk, seconds old, at the moment the replacement starts.
# The first probe of a container that came up and then wedged — `db.init()` blocking on a
# database the outgoing container still holds, say — would read that mark, find it fresh, and
# report `healthy` for work another container did. That is precisely the failure this
# healthcheck exists to catch.
#
# /tmp is in the container's own writable layer, and the daemon creates that layer EMPTY when it
# CREATES a container. An updater REPLACES the container, so the mark cannot cross from the
# retired one to its replacement — that whole scenario is closed by the location alone.
# (Two claims there, both verified on nebula — Docker 27.1.1, the version production runs — and
# the second one is narrower than it is usually written: it is the LAYER that is empty. `/tmp` as
# seen inside the container is that layer over the image's own /tmp, so an image shipping files in
# /tmp would show them. python:3.x-slim was checked and ships an empty /tmp, which is why the two
# coincide here — but that is a property of the base image, not a law.)
#
# WHAT THE LOCATION DOES NOT COVER, and this is the half that gets forgotten: the layer survives
# a RESTART of the same container. `docker restart`, `restart: unless-stopped` after the process
# dies, `docker stop && docker start`, a host reboot — all of them start a new process over the
# SAME writable layer, with the previous run's mark still in it, at most HEARTBEAT_INTERVAL old.
# Docker resets the health state to `starting` on a restart (verified on nebula, 27.1.1), so the new run is
# graded from scratch: its first probe reads that inherited mark, finds it fresh, and reports
# healthy a process that is at that moment wedged in db.init(). clear_heartbeat(), called from
# main.py before anything that can block, is what closes THAT — see its docstring.
#
# So do not move this next to the database to keep it "with the data". The mark is not data:
# it is worth nothing a second after the process that wrote it stopped, it must not outlive the
# container, and the only reader is a probe running inside that same container. Putting it on
# the volume reopens the hole above, and no content of the file can close it again — in
# particular NOT the writer's PID, which is the version this code carried until review pointed
# out that every container has its own PID namespace, so the retired container's `python
# main.py` had written the very same `1` the check would have been looking for.
#
# The one known cost: a container run with a read-only root filesystem and no tmpfs at /tmp
# cannot write the mark at all. The stack uses neither, and if one ever did, write_first_heartbeat()
# below makes that stop the container with the reason on its last line rather than leave it
# silently un-healthy forever.
DEFAULT_HEARTBEAT_FILE = "/tmp/heartbeat"

# How often src/scheduler.py rewrites the mark. It is a job of its own — one write, no network
# and no database — precisely so that the number below can be trusted; see the comment on that
# job for what sharing the reminder job's schedule used to cost.
HEARTBEAT_INTERVAL = 30

# 90 s = three missed ticks of the 30 s heartbeat job. One missed tick proves nothing (a slow
# await, a clock adjustment, a job that fired a second late) and would make the probe flap;
# three in a row is a loop that has stopped turning. The number IS three of that job's
# intervals, so it moves only if HEARTBEAT_INTERVAL moves.
# WHAT RAISING IT WOULD COST, stated exactly, because the obvious guess is wrong. It is NOT
# about the ~120 s window Portainer's updater waits for `healthy`. A container that wedges
# right after its startup mark reports healthy at its first probe, about 5 s in, and holds that
# verdict through the whole window at 90 exactly as it would at 180: `unhealthy` takes three consecutive
# failed probes AFTER the mark goes stale, so the earliest it can be graded is 90 + up to 30 to the
# first probe + 60 for the other two = 150-180 s, past the window either way. A tolerance below the
# window would not save that container either, only shorten the lie.
# What the number really buys is DETECTION SPEED in production, on a container that has been up
# for hours: 150-180 s after its last successful mark at this tolerance, against 240-270 s at
# the 180 s this file used to carry.
HEARTBEAT_MAX_AGE = 90


def heartbeat_file():
    """Resolve where the mark lives.

    HEARTBEAT_FILE wins outright. That override exists so the probe can be pointed at a staged
    file and tested (the CI smoke gate does exactly that), and so a developer running the bot
    outside a container can keep the mark out of /tmp. An empty HEARTBEAT_FILE falls back to
    the default rather than resolving to "", because an environment variable that is declared
    and left blank is how compose spells "not set".

    It takes no argument and reads nothing else on purpose: the mark must not follow
    DATABASE_PATH. Deriving it from the database path is what used to put it on the volume,
    and DEFAULT_HEARTBEAT_FILE explains at length why that cannot be allowed back.

    NOTE for the override: a HEARTBEAT_FILE pointing at a mounted path (a volume, a bind mount)
    gives up even the recreate half of the guarantee above — the mark then survives the container
    itself. clear_heartbeat() covers that case as well as the restart case it exists for.
    """
    return os.getenv("HEARTBEAT_FILE") or DEFAULT_HEARTBEAT_FILE


def format_mark(timestamp):
    """The ONE definition of what goes in the file: the unix time, and nothing else.

    Exposed rather than inlined into write_heartbeat() because the CI smoke gate stages a mark
    of its own — one aged past the limit — and a second spelling of the format over there would
    let the two drift apart while both went on looking right.
    """
    return "{}".format(int(timestamp))


class HeartbeatUnwritable(RuntimeError):
    """The STARTUP mark could not be written. Raised by write_first_heartbeat() only.

    It exists because the failure it reports is silent in the worst possible direction. A
    container whose /tmp is not writable — a read-only root filesystem with no tmpfs, a volume
    mounted over /tmp, a full disk — runs the bot perfectly and never writes a mark, so the probe
    reports `unhealthy` forever and the updater rolls back a GOOD image, over and over, with a
    warning every 30 s as the only trace. src/scheduler.py names that direction of wrongness as
    worse than having no probe at all; this is the class that stops it being reachable in silence.
    """


def _write_mark(path):
    """The write itself, shared by both callers below.

    WRITTEN THROUGH A TEMPORARY FILE AND os.replace(), which is atomic on POSIX. A plain
    `open(path, "w")` truncates first and writes after, leaving a window in which the file is
    empty — and the reader is a probe that fires every 30 s against a file rewritten every 30 s,
    so it is a window that will eventually be hit. It would report a malformed mark, i.e.
    `unhealthy`, on a container doing exactly what it should. The temporary file is in the same
    directory deliberately: os.replace() is only atomic within one filesystem.
    """
    temporary = path + ".new"
    with open(temporary, "w") as handle:
        handle.write(format_mark(time.time()))
    os.replace(temporary, path)


def write_heartbeat(path, logger=None):
    """The PERIODIC mark: best-effort, because heartbeat I/O must never break the job writing it.

    A raise here would propagate out of the job and be swallowed by APScheduler, which would
    cost the heartbeat over a full disk without saying so. A warning in the log and a stale
    mark say the same thing far more usefully: the probe will report it.

    This is the right trade for the SECOND write onwards — by then the mark has been written
    once, so the path is known to work and a failure here is a change of circumstances the probe
    is entitled to grade. The FIRST write is a different question and has its own function.
    """
    try:
        _write_mark(path)
    except Exception as error:  # noqa: BLE001 - a failed periodic heartbeat must not raise
        if logger is not None:
            logger.warning("Failed to write heartbeat {}: {}", path, error)


def write_first_heartbeat(path):
    """The STARTUP mark, and this one is LOUD: it raises HeartbeatUnwritable.

    Deliberately NOT best-effort like write_heartbeat() above, because the two failures are not
    the same failure. A periodic write that fails after the mark has existed once makes the mark
    go stale, which the probe reports and which is the correct verdict for a container whose disk
    filled up. A FIRST write that fails means the mark never existed at all — and "never healthy"
    is indistinguishable, to Portainer's updater, from a broken image, so a perfectly working bot
    gets rolled back on every deploy while its only complaint is a warning line every 30 s.

    Dying instead turns that into a container that stops, with the reason on its last line. The
    verdict is the same either way (the update does not stick); what changes is whether anybody
    can tell why.

    The message names the path and the fix, and nothing else — no secret is in scope here.
    """
    try:
        _write_mark(path)
    except Exception as error:  # noqa: BLE001 - re-raised as HeartbeatUnwritable below
        raise HeartbeatUnwritable(
            "cannot write the startup heartbeat to {}: {}. The health probe reads that file and "
            "nothing else, so this container would never report healthy however well the bot "
            "worked — and an image whose container does not reach `healthy` is exactly what "
            "Portainer's updater rolls back. Give the container a writable /tmp (a tmpfs entry "
            "will do) or point HEARTBEAT_FILE at a path it can write.".format(path, error)
        ) from error


def clear_heartbeat(path, logger=None):
    """Drop whatever mark is already at `path`, at the very start of a process.

    THIS IS LOAD-BEARING ON THE DEFAULT PATH, and it is worth being exact about why, because the
    obvious reading of DEFAULT_HEARTBEAT_FILE ("the layer is created empty, so there is never
    anything to delete") is wrong in one specific and entirely ordinary case. The layer is new
    when the container is CREATED — that is the updater's case and it needs nothing from here.
    But the layer SURVIVES A RESTART of the same container: `docker restart`, `restart:
    unless-stopped` after the process falls over, `docker stop && docker start`, a host reboot.
    The new run then starts with the previous run's mark on disk, at most HEARTBEAT_INTERVAL
    old, while docker has reset the health state to `starting` and is probing from scratch — so
    the first probe reads a mark that is fresh by its standard and reports healthy a process
    that has got no further than db.init(). One os.remove() here is the whole of the defence
    against that.

    It covers the HEARTBEAT_FILE override into the bargain: pointed at a volume or a bind mount,
    the mark survives recreation too, and the same call removes it.

    It has to run before anything that can block: a startup that hangs after this call leaves
    no mark at all, which is exactly the verdict wanted.

    Best-effort, for the same reason write_heartbeat() is: a read-only or full volume is a
    problem to report, not a reason to refuse to start.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        if logger is not None:
            logger.warning("Failed to remove inherited heartbeat {}: {}", path, error)


def read_heartbeat(path):
    """Return the unix time the mark was written at.

    Raises OSError when the file is missing or unreadable, and ValueError when it holds
    anything other than the single integer format_mark() writes. The caller has to keep those
    apart: a missing mark is a container that has not ticked yet, a malformed one is a writer
    that is broken — and both are "not healthy", but only one of them is normal in the first
    seconds of a container's life.
    """
    with open(path) as handle:
        content = handle.read().strip()
    parts = content.split()
    if len(parts) != 1:
        raise ValueError("expected a single unix time, found {!r}".format(content[:80]))
    return int(parts[0])
