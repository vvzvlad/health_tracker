"""The heartbeat file: written by the heartbeat job, read by the Docker HEALTHCHECK probe.

It is the single place the heartbeat contract is defined, and it is deliberately STDLIB-ONLY:
src/healthcheck.py imports it, and going through `src.settings` would make the probe fail with
a ValidationError when TELEGRAM_BOT_TOKEN is absent, masking the liveness verdict.

The file holds one number, the unix time it was written at. Nothing identifies the writer; see
DEFAULT_HEARTBEAT_FILE for why the location alone settles that.
"""

import os
import time

# Where the mark lives: /tmp, the container's own writable layer, NOT the data volume.
#
# Portainer's updater replaces the container over the same data volume, so a mark on the volume
# would still be fresh when the replacement starts, and a replacement wedged in `db.init()`
# would be reported healthy. The writable layer is created empty with each container (verified
# on Docker 27.1.1; it is the layer that is empty, and python:3.x-slim ships an empty /tmp), so
# no mark can cross to a replacement. No file content (e.g. a PID, which is 1 in every
# container's namespace) could provide that guarantee.
#
# The layer does survive a restart of the same container, while Docker resets health to
# `starting`; clear_heartbeat(), called from main.py before anything that can block, covers that.
#
# A read-only root filesystem without a tmpfs at /tmp cannot write the mark; the stack uses
# neither, and write_first_heartbeat() then stops the container with the reason.
DEFAULT_HEARTBEAT_FILE = "/tmp/heartbeat"

# How often src/scheduler.py rewrites the mark, from a job of its own (one write, no network,
# no database) so that the timing can be trusted.
HEARTBEAT_INTERVAL = 30

# Three missed ticks of the heartbeat job: one missed tick (a slow await, a late job) would make
# the probe flap. Moves only with HEARTBEAT_INTERVAL. It sets detection speed for a long-running
# container that wedges (150-180 s after the last mark, counting three failed probes); it cannot
# affect the ~120 s updater window, since `unhealthy` needs three failed probes after staleness.
HEARTBEAT_MAX_AGE = 90


def heartbeat_file():
    """Resolve where the mark lives.

    HEARTBEAT_FILE wins outright, so the probe can be pointed at a staged file (the CI smoke
    gate does this) and a bot run outside a container can keep the mark out of /tmp. An empty
    value falls back to the default, since compose spells "not set" as a blank variable.

    Deliberately not derived from DATABASE_PATH: the mark must not live on the data volume (see
    DEFAULT_HEARTBEAT_FILE). A HEARTBEAT_FILE on a mounted path survives the container itself;
    clear_heartbeat() covers that case too.
    """
    return os.getenv("HEARTBEAT_FILE") or DEFAULT_HEARTBEAT_FILE


def format_mark(timestamp):
    """The ONE definition of what goes in the file: the unix time, and nothing else.

    Exposed because the CI smoke gate stages an aged mark of its own and must use the same format.
    """
    return "{}".format(int(timestamp))


class HeartbeatUnwritable(RuntimeError):
    """The STARTUP mark could not be written. Raised by write_first_heartbeat() only.

    Without it a container with an unwritable /tmp (read-only root, volume over /tmp, full disk)
    runs fine, never writes a mark, and is rolled back as `unhealthy` on every deploy with only
    a warning in the log as a trace.
    """


def _write_mark(path):
    """The write itself, shared by both callers below.

    Written through a temporary file and os.replace() (atomic on POSIX, same directory so the
    same filesystem): a truncate-then-write would leave an empty file that the 30 s probe would
    eventually read as malformed.
    """
    temporary = path + ".new"
    with open(temporary, "w") as handle:
        handle.write(format_mark(time.time()))
    os.replace(temporary, path)


def write_heartbeat(path, logger=None):
    """The PERIODIC mark: best-effort, because heartbeat I/O must never break the job writing it.

    A raise would be swallowed by APScheduler anyway; a warning plus a stale mark that the probe
    reports says more. The first write has its own, loud function.
    """
    try:
        _write_mark(path)
    except Exception as error:  # noqa: BLE001 - a failed periodic heartbeat must not raise
        if logger is not None:
            logger.warning("Failed to write heartbeat {}: {}", path, error)


def write_first_heartbeat(path):
    """The STARTUP mark, and this one is LOUD: it raises HeartbeatUnwritable.

    Not best-effort like write_heartbeat(): a first write that fails means the mark never
    exists, the container never reports healthy, and Portainer's updater rolls back a working
    image with only a warning in the log. Dying puts the reason on the container's last line.
    No secret is in scope in the message.
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

    Load-bearing on the default path: the writable layer is new when a container is created,
    but survives a restart of the same container (`docker restart`, a restart policy, a host
    reboot). The new run would then start with the previous run's mark, at most
    HEARTBEAT_INTERVAL old, and the first probe would report healthy a process stuck in
    db.init(). It also covers a HEARTBEAT_FILE override pointing at a volume.

    Must run before anything that can block. Best-effort, like write_heartbeat(): a read-only
    or full volume is a problem to report, not a reason to refuse to start.
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
