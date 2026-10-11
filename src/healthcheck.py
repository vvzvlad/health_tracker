#!/usr/bin/env python3
"""Docker HEALTHCHECK probe for health_tracker.

Exit 0 (healthy) while the heartbeat is fresh; exit 1 (unhealthy) when it is missing, stale or
unreadable. The mark is written by a dedicated 30 s job in src/scheduler.py, so this grades the
asyncio loop still turning, not merely that a process exists.

Who wrote the mark is settled elsewhere: it lives in the container's own writable layer
(DEFAULT_HEARTBEAT_FILE in src/heartbeat.py), which is empty for a new container, and main.py
deletes any mark from a previous run of the same container (clear_heartbeat). Do not move the
mark to a shared volume: the probe would then grade the outgoing container's work.

Not graded: whether the bot serves anybody after startup. aiogram retries getUpdates forever,
so later polling failures show only in the log (see src/logging_setup.py). Startup failures are
caught: main.py makes its first Bot API request before the first mark is written, so a bad
address or token (including a black-holed address, after aiogram's 60 s timeout) kills the
process before it ever reports healthy, which is what Portainer's updater rolls back on.

Run as `python -m src.healthcheck` (WORKDIR /app), as the Dockerfile's HEALTHCHECK does.

Configuration is read from the environment through `src.heartbeat`, not `src.settings`:
`Settings` raises without TELEGRAM_BOT_TOKEN, and a configuration error must not mask the
liveness verdict.
"""

import sys
import time

from src.heartbeat import HEARTBEAT_MAX_AGE, heartbeat_file, read_heartbeat


def main():
    path = heartbeat_file()
    try:
        written_at = read_heartbeat(path)
    except OSError:
        # Missing or unreadable mark: normal for the first seconds of a container's life
        # (covered by --start-period and --retries) and before the first Bot API call succeeds.
        print(f"heartbeat file {path} missing", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"heartbeat file {path} is unreadable: {error}", file=sys.stderr)
        return 1
    age = time.time() - written_at
    if age > HEARTBEAT_MAX_AGE:
        print(f"heartbeat stale: {int(age)}s > {HEARTBEAT_MAX_AGE}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
