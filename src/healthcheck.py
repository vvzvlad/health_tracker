#!/usr/bin/env python3
"""Docker HEALTHCHECK probe for health_tracker.

Exit 0 (healthy) while the heartbeat is fresh AND was written by this container's own main
process; exit 1 (unhealthy) when it is missing, stale, unreadable, or inherited from the
container this one replaced. The mark is written by a dedicated 30 s job in src/scheduler.py
that does nothing else, so what this probe grades is the asyncio loop still turning — not
merely that a process exists. A bot whose event loop has wedged keeps its PID and stops
answering; that is the failure this makes visible.

WHAT IT DOES NOT GRADE: whether the bot is serving anybody. aiogram retries getUpdates forever
around a bare `except Exception`, so a permanent failure on the API side leaves the loop
turning, this probe green and the bot useless. That class of breakage shows up in the log
(see src/logging_setup.py), never here.

The PID row is what makes the file mean anything on a fresh container. The mark lives on the
data volume and the updater recreates the container over that same volume, so a mark written
seconds ago by the outgoing container is on disk when this one starts — and without the PID
check the very first probe of a container that came up and wedged would read it and report
healthy. main.py deletes the mark at startup for the same reason; this is the check that still
holds if the deletion did not.

Run as `python -m src.healthcheck` (WORKDIR /app in the image), which is exactly what the
Dockerfile's HEALTHCHECK line does.

Configuration is read straight from the environment through `src.heartbeat` rather than
through `src.settings`, and that is deliberate: `Settings` requires TELEGRAM_BOT_TOKEN and
raises when it is missing, so a probe built on it would report "unhealthy" for a
configuration reason and hide the liveness verdict it exists to give — handing Portainer's
updater a container that never goes healthy over a typo in the stack's `environment:` block,
which is precisely the rollback this healthcheck was added to make possible.
"""

import sys
import time

from src.heartbeat import (
    CONTAINER_MAIN_PID,
    HEARTBEAT_MAX_AGE,
    heartbeat_file,
    read_heartbeat,
)


def main():
    path = heartbeat_file()
    try:
        pid, written_at = read_heartbeat(path)
    except OSError:
        # Missing or unreadable mark. Legitimate for the first seconds of a container's
        # life, which is what --start-period and --retries in the Dockerfile are for — and
        # after main.py's startup deletion it is also the honest answer for a container that
        # has come up and never reached its scheduler.
        print(f"heartbeat file {path} missing", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"heartbeat file {path} is unreadable: {error}", file=sys.stderr)
        return 1
    if pid != CONTAINER_MAIN_PID:
        # An inherited mark: written by a process that is not this container's PID 1, i.e. by
        # the container this one replaced on the shared volume. Grading it fresh would report
        # healthy for work another container did.
        print(
            f"heartbeat was written by pid {pid}, not by this container's main process "
            f"(pid {CONTAINER_MAIN_PID}): it is left over from the previous container",
            file=sys.stderr,
        )
        return 1
    age = time.time() - written_at
    if age > HEARTBEAT_MAX_AGE:
        print(f"heartbeat stale: {int(age)}s > {HEARTBEAT_MAX_AGE}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
