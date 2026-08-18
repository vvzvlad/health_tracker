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
# layer, which the daemon creates empty for each container — so a mark left by the container
# this one replaced cannot be at that path and cannot report the new container healthy before
# it has ticked once. That property comes from WHERE the file is, not from anything in it; see
# DEFAULT_HEARTBEAT_FILE in src/heartbeat.py before moving it.
#
# WHAT IT PROVES IS NARROWER THAN "the image works", and the difference decides which bad
# images this rolls back. Green here means: the process is up and its scheduler is dispatching.
# It says NOTHING about the bot serving anybody ONCE STARTUP HAS SUCCEEDED. aiogram's
# `Dispatcher._listen_updates` catches every exception around getUpdates and retries forever, so
# a failure that arrives after that point — 409 because something else started polling the same
# bot, a revoked token, a Bot API server that goes away — leaves the process alive, the
# scheduler ticking, the mark fresh and this probe green while nobody is served. An image broken
# THAT way gets published, deployed and reported healthy. What makes it visible is the log, and
# only because src/logging_setup.py routes aiogram's stdlib logger into loguru instead of
# leaving it to `logging.lastResort`.
# A failure present AT startup is caught here rather than hidden: `HealthBot.start()` calls
# set_my_commands before it polls, so a wrong address or a refused connection raises, the
# process dies, and a container that keeps dying never reaches `healthy` — which is exactly the
# signal the updater rolls back on.
#
# THE TIMINGS ARE LOAD-BEARING, not taste. Portainer's updater waits roughly 120 s for a freshly
# deployed container to report `healthy` and rolls the image back if it does not, so what these
# four numbers really decide is whether an automatic update sticks.
#   * DURING --start-period docker probes every --start-interval (5 s by default, on Docker
#     >= 25.0) and the FIRST success marks the container healthy immediately. Measured on
#     this image: healthy 5.4 s after start, on Docker 29.7.2; the production daemon is on
#     27.1.1, which behaves the same. That fast path exists only because the scheduler writes
#     one mark at startup — the 30 s heartbeat job's own first run is a whole interval away,
#     so without that write the early probes find no file.
#   * A container that NEVER becomes healthy is only GRADED `unhealthy` at about
#     --start-period + --retries x --interval = 30 + 3 x 30 = 120 s, because failures inside
#     the start period do not increment the retry counter — the counter starts when the start
#     period ends. That is level with the updater's window rather than well inside it; what
#     saves the rollback is that it does not wait for the `unhealthy` verdict at all — it
#     fires because `healthy` never arrived within the window.
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