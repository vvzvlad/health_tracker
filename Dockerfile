FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ src/
COPY main.py .

# Create data directory
RUN mkdir -p data

# The liveness probe. This image had NO healthcheck at all, and that mattered more than it
# looks: the production container carries `io.portainer.update.enable=true`, so an updater
# deploys every new :latest by itself — and the updater's ROLLBACK of a bad image is
# triggered by the new container failing to reach `healthy`. With no healthcheck there is no
# `healthy` to reach and therefore no rollback to hope for: an image that dies on startup
# stays deployed and dead until a human notices the bot went quiet.
# It reads the heartbeat written by a job that does nothing else, every 30 s (src/heartbeat.py,
# src/scheduler.py), so the verdict is about the asyncio loop still turning rather than about a
# process still existing. The mark is written to /tmp, i.e. into the container's own writable
# layer, which the daemon creates empty when it CREATES a container — so a mark left by the
# container this one REPLACED cannot be at that path. That half comes from WHERE the file is and
# from nothing in it. The other half is not free: the same layer survives a RESTART of this
# container (`docker restart`, `restart: unless-stopped`, a host reboot), which would hand the
# new run the previous run's mark, so main.py deletes any mark it finds before it does anything
# that can block. Read DEFAULT_HEARTBEAT_FILE and clear_heartbeat in src/heartbeat.py before
# moving either.
#
# WHAT IT PROVES IS NARROWER THAN "the image works", and the difference decides which bad
# images this rolls back. Green here means: the process is up, it reached its Bot API once and
# was accepted, and its scheduler is dispatching.
# It says NOTHING about the bot serving anybody ONCE STARTUP HAS SUCCEEDED. aiogram's
# `Dispatcher._listen_updates` catches every exception around getUpdates and retries forever, so
# a failure that arrives after that point — 409 because something else started polling the same
# bot, a revoked token, a Bot API server that goes away — leaves the process alive, the
# scheduler ticking, the mark fresh and this probe green while nobody is served. An image broken
# THAT way gets published, deployed and reported healthy. What makes it visible is the log, and
# only because src/logging_setup.py routes aiogram's stdlib logger into loguru. Without that
# bridge such a record has no handler on its chain at all: below WARNING the logger drops it
# before it is even built (the effective level is root's default of WARNING), and at WARNING and
# above it leaves through `logging.lastResort` — unformatted, unattributed and unredacted.
# A failure present AT startup is caught here rather than hidden, and that includes the slow
# kind, which it did not until the call order was fixed. main.py calls HealthBot.contact_api()
# — the first Bot API request — BEFORE scheduler.start(), and the scheduler's start is what
# writes the first mark. So no mark exists until the bot has been answered by its server: a
# refused connection, an address that resolves nowhere or a 401 kills the process at once, and an
# address that merely black-holes traffic (a wrong IP on the right subnet, a closed port, a host
# that is down — the ordinary way this gets misconfigured on a LAN) holds the request for
# aiogram's 60 s per-request timeout and then kills it. Neither ever reports healthy.
# With the API call BELOW the scheduler, as it was, the black-hole case was not caught at all: the
# mark appeared within a second or two of startup, so the container was healthy at its first probe
# (~5 s) and stayed healthy for the ~60 s until the timeout fired — covering the updater's window.
# The broken image was accepted, and under `restart: unless-stopped` it was re-accepted for as long
# as anybody left it alone.
# The process must also DIE rather than hang when that request fails, which is not free: see the
# comment on the `finally` in main.py, and the aiosqlite thread it is there for.
#
# THE TIMINGS ARE LOAD-BEARING, not taste. Portainer's updater waits roughly 120 s for a freshly
# deployed container to report `healthy` and rolls the image back if it does not, so what these
# four numbers really decide is whether an automatic update sticks.
#   * THE PROBE SCHEDULE, measured on nebula — Docker 27.1.1, the version production runs — with
#     exactly the four numbers below and a check that always fails, read out of the container's own
#     State.Health.Log: probes at +20.3, +25.4, +30.4, +60.5, +90.5 s. Those five are the LAST five
#     and five is ALL THERE IS — docker keeps only the five most recent entries in that log, so the
#     probes at +5, +10 and +15 s had already been pushed out of it by the time it was read. The
#     first probe is therefore measured separately, by inspecting a container before the eviction:
#     +5.2 s. (Both re-measured on the same host while this paragraph was corrected: probes at
#     +5.2, +10.2, +15.3, +20.3, +25.4, +30.5, +60.5, +90.6 s, with the log stuck at five entries
#     from the sixth probe onwards.) Read that as: docker probes every
#     --start-interval WHILE --start-period is running (5 s, the documented default since Docker
#     25, and it applies WITHOUT the flag being declared — a container given an explicit
#     --health-start-interval=5s first-probed at +5.3 s against +5.2 s without it, i.e. no
#     difference), then falls back to --interval, 30 s, once the start period ends. The FIRST
#     success marks the container healthy, so a good container is healthy at about 5 s on any
#     daemon >= 25. Other park hosts run 28.3.1 and 29.x and were not measured; nothing here
#     depends on a version-specific quirk, only on that documented default.
#     DELIBERATELY NOT DECLARING --start-interval=5s: the default already gives 5 s on every daemon
#     we run, and pinning it explicitly would buy version-independence at the price of one more
#     expectation to keep in step in ci/smoke.py.
#     The startup mark is what makes that 5 s real. Without it the first mark would be the
#     heartbeat job's own first run, one whole interval after the scheduler starts, i.e. ~32 s in —
#     past every start-period probe — so the first passing probe would be the +60 s one and
#     `healthy` would slip from ~5 s to ~60 s, half the updater's window.
#   * WHAT THE CALL-ORDER FIX COSTS, against that schedule. The mark now appears at (startup + one
#     Bot API request, call it R) rather than at (startup), and `healthy` at the first probe at or
#     after it. With R in milliseconds — the local Bot API server on the LAN — NOTHING MOVES: the
#     mark is on disk before the +5 s probe, exactly as before. R has to exceed ~28 s to push the
#     mark past the start period at all, and it cannot exceed 60 s without raising, because that is
#     aiogram's per-request timeout and there is one attempt, no retry. So the worst SUCCESSFUL
#     start is a mark at ~62 s picked up by the +90 s probe: inside the ~120 s window with about
#     30 s to spare. Anything slower than aiogram's timeout is not a slow start, it is a start that
#     failed — and that is the verdict wanted.
#   * A container that NEVER becomes healthy is GRADED `unhealthy` at about 90 s. The rule is
#     that failures inside the start period do not increment the retry counter, but the naive
#     arithmetic that follows from it — --start-period + --retries x --interval = 120 s — is
#     wrong by a whole --interval: the streak starts at the BOUNDARY probe itself, not one full
#     --interval after the start period ends. The 5 s grid crosses 30 s and its first probe past
#     the boundary already counts. Measured on nebula (Docker 27.1.1, always-failing probe): the
#     streak reads 0 at +26 s, 1 at the +30.5 s probe, 2 at +62 s, and 3 with the `unhealthy`
#     verdict at +90.6 s. The sub-second offset is not what buys the 30 s — it only guarantees
#     that the boundary probe lands after 30.0 s rather than on it, and it can only grow, since
#     docker waits --start-interval after each probe FINISHES. So the verdict lands ~30 s INSIDE
#     the updater's window, not level with it. The rollback does not depend on this either way:
#     it fires because `healthy` never arrived, not because `unhealthy` did.
#   * A container that WAS healthy and then wedges is caught by two numbers that MULTIPLY
#     rather than add: the mark has to go stale first (HEARTBEAT_MAX_AGE = 90 s, three missed
#     ticks of the 30 s job) and only then must three consecutive --interval probes fail. That
#     lands the verdict 150-180 s after the last successful heartbeat write, which is itself at
#     most 30 s before the loop stopped. Raising EITHER number moves that whole product: at the
#     180 s tolerance this file used to carry, the same detection took ~270 s.
#   * The trap is the "the service is stable, probe it every five minutes" edit: at
#     --interval=5m a container that merely started slowly reports healthy after five and a
#     half minutes, past the window, and every automatic update rolls itself back — silently,
#     and looking like a failing image.
# --timeout=5s is generous for a python start-up that does one open() and one read().
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -m src.healthcheck || exit 1

# Run the bot
CMD ["python", "main.py"]