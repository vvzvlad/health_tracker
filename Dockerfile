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
# It reads the heartbeat the reminder job writes once a minute (src/heartbeat.py), so the
# verdict is about the asyncio loop still turning rather than about a process still existing.
#
# THE TIMINGS ARE LOAD-BEARING, not taste, and the arithmetic below is measured rather than
# assumed. Portainer's updater waits roughly 120 s for a freshly deployed container to report
# `healthy` and rolls the image back if it does not, so what these four numbers really decide
# is whether an automatic update sticks.
#   * DURING --start-period docker probes every --start-interval (5 s by default, on Docker
#     >= 25.0) and the FIRST success marks the container healthy immediately. Measured on
#     this image: healthy 5.4 s after start, on Docker 29.7.2; production nebula.lc runs
#     27.1.1, which behaves the same. That fast path exists only because the scheduler
#     writes one mark at startup — without it those early probes find no file and it is lost.
#   * If the probe keeps failing through the start period, the next verdict comes one full
#     --interval later. At 30 s that is still well inside the window. The trap is the
#     "the service is stable, probe it every five minutes" edit: at --interval=5m a
#     container that merely started slowly reports healthy after five and a half minutes,
#     past the window, and every automatic update rolls itself back — silently, and looking
#     like a failing image.
# --retries=3 covers a probe that misses a tick, which is also what HEARTBEAT_MAX_AGE is
# built from; --timeout=5s is generous for a python start-up that does one stat().
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -m src.healthcheck || exit 1

# Run the bot
CMD ["python", "main.py"]