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

# Liveness probe. The production container carries `io.portainer.update.enable=true`, and the
# updater rolls back a new image whose container never reaches `healthy`, so this probe is what
# makes that rollback possible.
#
# It reads the mark written every 30 s by a job that does nothing else (src/heartbeat.py,
# src/scheduler.py), so it grades the asyncio loop, not just the process. The mark lives in /tmp,
# the container's own writable layer, which is empty on container creation; main.py deletes any
# mark left by a previous run of the same container (the layer survives restarts). Read
# DEFAULT_HEARTBEAT_FILE and clear_heartbeat in src/heartbeat.py before moving either.
#
# Green means: the process is up, its first Bot API request succeeded, and the scheduler is
# dispatching. main.py calls HealthBot.contact_api() before scheduler.start() writes the first
# mark, so a refused/unresolvable address or a 401 kills the process at once, and a black-holed
# address does so after aiogram's 60 s request timeout; neither ever reports healthy (the process
# must also die rather than hang: see the `finally` in main.py). Green says nothing about polling
# failures after startup (409, revoked token, Bot API server gone): aiogram retries getUpdates
# forever, so those show only in the log (src/logging_setup.py bridges aiogram's logger).
#
# The timings are load-bearing: Portainer's updater waits ~120 s for `healthy`.
#   * Docker probes every --start-interval (default 5 s since Docker 25) during --start-period,
#     then every --interval. The first success marks the container healthy, so a good container
#     is healthy at ~5 s (measured on Docker 27.1.1). --start-interval is left at its default to
#     avoid one more value to keep in step with ci/smoke.py. The startup mark makes this work:
#     without it the first mark would come one interval (~32 s) in and `healthy` at ~60 s.
#   * The mark appears after one Bot API request (R). R up to ~28 s changes nothing; R cannot
#     exceed 60 s (aiogram's timeout, one attempt), so the worst successful start is healthy at
#     the +90 s probe, inside the window.
#   * A container that never becomes healthy is graded `unhealthy` at ~90 s, not 120 s: the
#     failure streak starts at the first probe past the start period (measured: 1 at +30.5 s,
#     3 at +90.6 s). The rollback fires on missing `healthy`, not on `unhealthy`.
#   * A container that wedges after being healthy is caught 150-180 s after its last mark:
#     HEARTBEAT_MAX_AGE (90 s) to go stale, then three failed --interval probes. Raising either
#     number lengthens that.
#   * Do not raise --interval to minutes: a slow-starting container would report healthy past
#     the updater's window, and every automatic update would roll itself back.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -m src.healthcheck || exit 1

# Run the bot
CMD ["python", "main.py"]