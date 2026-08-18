#!/usr/bin/env python3
"""Docker HEALTHCHECK probe for health_tracker.

Exit 0 (healthy) while the heartbeat is fresh; exit 1 (unhealthy) when it is missing, stale or
unreadable. The mark is written by a dedicated 30 s job in src/scheduler.py that does nothing
else, so what this probe grades is the asyncio loop still turning — not merely that a process
exists. A bot whose event loop has wedged keeps its PID and stops answering; that is the
failure this makes visible.

THERE IS NOTHING HERE ABOUT WHO WROTE THE MARK, and that is split between two mechanisms, both
of which live elsewhere. The mark is at a fixed path in the container's own writable layer
(src/heartbeat.py, DEFAULT_HEARTBEAT_FILE), which the daemon creates empty when it CREATES a
container — that is what stops an updater's replacement container from being graded on the
retired one's work. It does NOT stop the same container's previous run from being graded: the
layer survives `docker restart` and a restart policy, so main.py deletes any mark it finds before
anything that can block (src/heartbeat.py, clear_heartbeat). Between the two, a mark this probe
finds was written by the run it is grading, and freshness is the whole remaining question. Read
the comment on DEFAULT_HEARTBEAT_FILE before moving the mark anywhere else: on a shared volume
this probe would report `healthy` for the outgoing container's work, and no check of the file's
CONTENT can fix that.

WHAT IT DOES NOT GRADE: whether the bot is serving anybody. Once startup has succeeded, aiogram
retries getUpdates forever around a bare `except Exception`, so a failure that arrives later — a
409 because something else started polling the same bot, a revoked token, a Bot API server that
goes away — leaves the loop turning, this probe green and the bot useless. That class of
breakage shows up in the log (see src/logging_setup.py), never here.

A failure present at STARTUP is a different story and IS caught, including the slow kind. main.py
makes its first Bot API request (HealthBot.contact_api) before the scheduler writes the first
mark, so no mark exists until the bot has been answered by its server. An address that refuses
the connection, resolves nowhere or returns 401 kills the process outright; an address that
merely black-holes traffic — a wrong IP on the right subnet, a closed port, a host that is down —
holds the request until aiogram's per-request timeout (60 s, one attempt) and then kills it too.
Either way the container never reports healthy, which is the signal Portainer's updater rolls
back on. That ordering is the ONLY reason the second case is covered: with the first API call
below the scheduler, the mark was on disk a second or two into the container's life, so the
container was green at its first probe (~5 s in) and stayed green for the whole 60 s the request
took — i.e. right through the updater's window.

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

from src.heartbeat import HEARTBEAT_MAX_AGE, heartbeat_file, read_heartbeat


def main():
    path = heartbeat_file()
    try:
        written_at = read_heartbeat(path)
    except OSError:
        # Missing or unreadable mark. Legitimate for the first seconds of a container's
        # life, which is what --start-period and --retries in the Dockerfile are for — and it
        # is also the honest answer for a container that has come up and not yet got past its
        # first Bot API call, since nothing writes a mark before that succeeds.
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
