#!/usr/bin/env python3
"""Docker HEALTHCHECK probe for health_tracker.

Exit 0 (healthy) while the heartbeat written by the reminder job is fresh; exit 1
(unhealthy) when it is missing or stale. The mark is written at the TOP of the once-a-minute
cron job in src/scheduler.py, so what this probe grades is the asyncio loop still turning —
not merely that a process exists. A bot whose event loop has wedged keeps its PID and stops
answering; that is the failure this makes visible.

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

from src.heartbeat import HEARTBEAT_MAX_AGE, heartbeat_age, heartbeat_file


def main():
    path = heartbeat_file()
    try:
        age = heartbeat_age(path)
    except OSError:
        # Missing or unreadable mark. Legitimate for the first seconds of a container's
        # life, which is what --start-period and --retries in the Dockerfile are for.
        print(f"heartbeat file {path} missing", file=sys.stderr)
        return 1
    if age > HEARTBEAT_MAX_AGE:
        print(f"heartbeat stale: {int(age)}s > {HEARTBEAT_MAX_AGE}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
