"""Smoke gate for the health_tracker image, run on the CI runner against an already-built image.

It sits BETWEEN `docker build` and `docker push`, which is the only position in which it is worth
anything: nobody presses a button between the push and the rollout. The production container carries
`io.portainer.update.enable=true`, so an updater polls `:latest` and deploys whatever lands on it.
This gate is the last point at which a broken image can still be stopped.

THERE ARE NO TESTS IN THIS REPOSITORY. Every dependency in requirements.txt is pinned, so nothing
drifts on its own — but nothing checks the source either, which puts the entire weight on this file.
What it is therefore built to catch is what actually breaks a small aiogram bot in practice: a symbol
that moved in aiogram, a handler that quietly stopped being registered, a database migration that
fails on a real file, a probe that is green whatever happens, and a container that never reaches its
first line of log.

THE SPLIT
---------
The OUTER half is this file: a plain `python3 ci/smoke.py` on the runner, driving `docker`. It
answers the questions that are about the container as an OBJECT — what `docker inspect` says its CMD,
WORKDIR and HEALTHCHECK are, and what the REAL command writes to its log when it is started the way
production starts it.

The INNER half is one PROBE: a program that lives here as a string constant and is fed to
`docker run -i --rm --network none <image> python -u -`. It answers everything about the code inside
the image — imports, aiogram symbols, handler registration, the database schema, the scheduler, the
health probe and the settings.

CONSTRAINTS OF THIS RUNNER, and they shape the whole file
---------------------------------------------------------
The job executes INSIDE act_runner's own job container while the `docker` CLI it provides drives the
daemon OUTSIDE it. Two consequences, both of which turn a careless check into one that passes by
looking at nothing:

* NO PORT IS PUBLISHED by anything here. This service has no HTTP surface at all, so there is nothing
  to publish — but were there, the published port would land in the HOST daemon's network namespace,
  and this job's 127.0.0.1 is a different loopback entirely.
* NOTHING IS BIND-MOUNTED. A `-v $(pwd):/w` would hand the daemon a path that exists in this
  container's filesystem and not in its own; it would mount an empty directory and every file check
  would pass against nothing. The probe therefore goes in on STDIN, and the one file this gate reads
  out of a container comes back through `docker cp`, which streams over the API rather than through a
  shared filesystem.

EVERY CONTAINER THIS GATE STARTS RUNS WITH `--network none`, and that is load-bearing twice over.
It guarantees the gate never reaches Telegram with the fake token below — so this same gate can run
on a pull request — and it turns "constructing a Bot performs no network I/O" from an assertion into
something the environment enforces: if `Bot(...)` ever started dialling out at construction time, the
probe would fail rather than pass quietly.

THE CHECKS
----------
* (a) the image's declared contract: CMD, WORKDIR, and the HEALTHCHECK with its exact timings. The
      timings are the mechanism by which a bad automatic update rolls itself back, so they are pinned
      to the second rather than merely checked for existence. Outer half.
* (b) the real CMD reaches its first lines of log, in BOTH branches of the endpoint decision: once
      with no Bot API server configured and once with TELEGRAM_BOT_API_SERVER set. The startup
      heartbeat is checked in ONE of those two containers — the cloud one, whose file is pulled out
      with `docker cp` and read. The local-server container is started for the other branch of the
      log line and is not asked about the file; the write is the same code either way. Both have
      their logs re-read at the end and searched for the token. Outer half.
* (c) every module under src/ imports, plus main. Probe.
* (d) every symbol the source imports out of aiogram (and out of the other third-party packages)
      exists, and so do the methods called on them. Probe.
* (e) every handler is registered against a REAL Dispatcher/Router — not a mock — and the set of
      registered callbacks is exactly the expected one. Probe.
* (f) THE ENDPOINT REGRESSION TEST. With TELEGRAM_BOT_API_SERVER set, the constructed bot's
      `session.api.base` really names that server; without it, api.telegram.org. This is checked on
      the SESSION and not on the setting, because the bug being guarded against was precisely a
      correctly-read setting that changed nothing: aiogram 3 takes `(token, session, default,
      **kwargs)` and silently discarded the `base_url=` the old code passed it. A check that stopped
      at "the setting was read" would have been green on the broken code. Probe.
* (g) the database schema is created on a real file and both migrations run: the `ALTER TABLE` that
      adds `metrics.description`, and the rebuild of `records` whose CHECK constraint still says
      BETWEEN 0 AND 5. The rebuilt table is checked to have kept its rows. Probe.
* (h) the scheduler registers exactly two jobs — the reminder cron with an explicit misfire grace,
      and the heartbeat interval job that is separate from it precisely so that a slow reminder
      round cannot stop the mark — and writes the startup heartbeat. Probe.
* (i) the heartbeat and the probe go round: a fresh mark exits 0, a mark aged past the limit exits
      non-zero, a missing mark exits non-zero, a mark bearing another process's PID exits non-zero
      (that is the inherited mark an updater leaves on the volume), and the default path really is
      the one next to the database. BOTH directions of each, because a probe that is always green
      gates nothing. Probe.
* (j) the settings guard: with no TELEGRAM_BOT_TOKEN the process fails with a message that names the
      field, rather than dying of an AttributeError somewhere deeper. A Bot API server address with
      no scheme is rejected, and the rejection does not carry the token. Both accepted names for the
      server resolve to the same value, with the park's convention winning when both are set. And
      the log redaction really removes a token from text. Probe.
* (k) the two workflows really do run the same lines in the steps that claim to be duplicates of
      each other. This is the only check here about the CI files rather than the image, and it is
      here because the claim is otherwise unenforceable: the PR gate and the publishing gate drift
      silently, and always in the direction of the PR gate testing less. Outer half, reading the
      checkout.

Three properties matter and are easy to lose, so they are stated where they can be checked:

* Failures leave through SystemExit, never `assert` — on both sides of the split. Asserts vanish
  under PYTHONOPTIMIZE=1, which would silently turn this gate permanently green.
* THE PROBE TEXT IS ASCII-ONLY. It is handed to `subprocess.run(input=..., text=True)`, which
  encodes it with the runner's preferred encoding — so a single em dash in it would make the gate
  die of UnicodeEncodeError under LC_ALL=C, before a container was ever started, and the failure
  would read like a broken gate rather than a broken image. This file's own comments are outside
  that constraint: python source is decoded as UTF-8 whatever the locale says.
* Every check runs before the run is judged, so one run shows the full extent of the breakage instead
  of only the first broken thing. A check that CANNOT run reports itself as FAILED; it is never
  quietly skipped, which is the classic way a gate keeps reporting success while proving less and
  less. The probe's own report lines are parsed back into this gate's report, so a failure inside the
  container is one row in the same list as a failure outside it.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

# The tag to test. Required rather than defaulted: a default would let a mistyped `env:` block in a
# workflow silently gate some other image that happens to be on the daemon.
IMAGE_ENV = "SMOKE_IMAGE"
# Base name for every container this gate starts. The workflows put the run id in it, because the
# runner has ONE docker daemon shared by every repository in the fleet and two concurrent runs must
# not collide on a name — remove_container() below would otherwise delete another run's live
# container out from under it. Required for a second reason too: the workflow's `if: always()`
# cleanup step builds the same names from the same variable, so a default here would leave this
# script naming its containers one way while the cleanup went looking for others, found nothing, and
# swallowed the miss in its `|| true`.
NAME_ENV = "SMOKE_NAME"

PROBE_SUFFIX = "-probe"
BOOT_CLOUD_SUFFIX = "-boot-cloud"
BOOT_LOCAL_SUFFIX = "-boot-local"
# Kept in step with the cleanup step in both workflows, which removes the same suffixes.
ALL_SUFFIXES = (PROBE_SUFFIX, BOOT_CLOUD_SUFFIX, BOOT_LOCAL_SUFFIX)

# ── what the image is supposed to declare ─────────────────────────────────────────────────────────
APP_DIR = "/app"
EXPECTED_CMD = ["python", "main.py"]
# `docker inspect` reports a shell-form HEALTHCHECK as CMD-SHELL plus the literal command line.
EXPECTED_HEALTHCHECK_TEST = ["CMD-SHELL", "python -m src.healthcheck || exit 1"]
# Nanoseconds, which is how the daemon reports durations. These four numbers are not style, and the
# Dockerfile explains them in full: Portainer's updater rolls an image back if `healthy` has not
# arrived in about 120 s. A healthy container reaches it in seconds, because docker probes every
# --start-interval (5 s by default) during the start period and the first success is enough; a
# container that never becomes healthy is graded `unhealthy` only at --start-period +
# --retries x --interval = 120 s, since failures inside the start period do not count towards the
# retries. A longer interval — the natural "it is a stable service" edit — pushes both of those past
# the window and makes every automatic update roll itself back while looking like a failing image.
# Pinned here so that edit cannot land without this gate saying so.
EXPECTED_HEALTHCHECK_INTERVAL = 30 * 1000000000
EXPECTED_HEALTHCHECK_TIMEOUT = 5 * 1000000000
EXPECTED_HEALTHCHECK_START_PERIOD = 30 * 1000000000
EXPECTED_HEALTHCHECK_RETRIES = 3

# ── the two workflows that must not drift apart ───────────────────────────────────────────────────
# Derived from this file's location, not from the working directory: the workflows run it as
# `python3 ci/smoke.py` from the checkout root today, and a relative path would quietly start
# reading nothing if that ever changed.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISH_WORKFLOW = os.path.join(REPO_ROOT, ".gitea", "workflows", "image-check-publish.yml")
TESTS_WORKFLOW = os.path.join(REPO_ROOT, ".gitea", "workflows", "tests.yml")
# The steps that exist in both files and are supposed to be the same rehearsal. Their COMMENTS
# differ deliberately; their executable lines must not.
DUPLICATED_STEPS = ("Compute image tags", "Smoke-test the built image")

# ── what a healthy boot looks like ────────────────────────────────────────────────────────────────
# A token that satisfies aiogram's `validate_token` (digits, a colon, a non-empty tail, no spaces)
# and belongs to no bot. Every container below runs with `--network none`, so it never leaves the
# daemon even if it wanted to.
FAKE_TOKEN = "123456789:AAFakeTokenUsedOnlyByTheSmokeGate0000"
# The address handed to the local-server boot. A PLACEHOLDER, deliberately, and not production's
# real internal address: this repository is public on two forges and the checks below work exactly
# as well against a name that resolves nowhere. Nothing connects to it in any case —
# `--network none` — and what is being checked is that the log line NAMES the endpoint it was given,
# whatever that endpoint is. The scheme is not decoration: src/settings.py rejects a bare host:port,
# so an address written without one would fail the boot rather than exercise the branch.
LOCAL_API_SERVER = "http://bot-api.example.lan:8081"

STARTUP_MARKER = "Starting Health Tracker Bot"
CLOUD_ENDPOINT_MARKER = "Bot API endpoint: cloud default api.telegram.org"
LOCAL_ENDPOINT_MARKER = "Bot API endpoint: local server {}".format(LOCAL_API_SERVER)
# The scheduler's own line, which also proves the startup heartbeat write below it was reached.
# The path in it is RELATIVE because DATABASE_PATH defaults to `data/health.db` and the scheduler
# logs the path it was given, not a resolved one. Pinned as the literal string the container really
# prints; the absolute location is pinned separately, by the `docker cp` row below, which is the one
# that proves where the file actually landed.
SCHEDULER_MARKER = "Scheduler started, heartbeat file data/heartbeat"
# Where that startup mark lands in the image: the relative path above, resolved against WORKDIR /app.
HEARTBEAT_IN_IMAGE = "/app/data/heartbeat"

# ── the probe ─────────────────────────────────────────────────────────────────────────────────────
PROBE_MARKER = "health_tracker image probe ok"
PROBE_ROW_PREFIX = "[in-image] "
# The number of verdicts the probe is supposed to print. Compared EXACTLY rather than "at least",
# because the failure this catches is a probe that quietly stopped checking things: every line it
# does print says ok and it exits 0, so nothing else in this file would notice.
# 9 modules + 26 imported symbols + 12 methods called on them + 6 handler registration
# + 5 Bot API endpoint + 8 fresh database + 6 legacy migration + 5 scheduler + 8 health probe
# + 8 settings.
EXPECTED_PROBE_TARGETS = 93

# The total this gate produces when everything runs: 20 of its own (8 contract + 2 workflow parity +
# 6 cloud boot + 4 local boot) + the probe's rows + the two consistency rows run_probe() adds about
# the probe itself.
EXPECTED_TOTAL_TARGETS = 20 + EXPECTED_PROBE_TARGETS + 2

# ── bounds ────────────────────────────────────────────────────────────────────────────────────────
# Every docker call is bounded, and the OUTER bound has to exceed the sum of the inner ones or it
# fires first and replaces a per-row diagnosis with "did not finish". The worst case, all of them
# hit at once:
#     inspect                                                                       30
#     probe          remove 30 + run 420                                            450
#     boot cloud     remove 30 + run 60 + markers 90+30 + cp 30 + token logs 30     270
#     boot local     remove 30 + run 60 + markers 90+30 + token logs 30             240
#     cleanup        3 x remove 30                                                   90
#                                                                                 -----
#                                                                       1080 s = 18 minutes
# (The markers line is BOOT_BUDGET plus one LOGS_TIMEOUT: wait_for_markers checks its deadline
# after each `docker logs`, so a call started just before the deadline still runs to its own bound.)
# The step's `timeout-minutes` in BOTH workflows is 25, set against this sum: the margin is there so
# that a gate which is merely slow fails with ITS OWN diagnosis rather than being killed by
# act_runner, which would skip the cleanup in the `finally` below. Adding a bounded call here
# without revisiting both workflows is how a gate starts being killed instead of reporting.
INSPECT_TIMEOUT = 30
REMOVE_TIMEOUT = 30
START_TIMEOUT = 60
LOGS_TIMEOUT = 30
COPY_TIMEOUT = 30
# 420, not 300. The probe runs subprocesses of its own and they are bounded too: six runs of
# `python -m src.healthcheck` at HEALTHCHECK_RUN_TIMEOUT (30 s) plus one Settings import at 60 s
# come to 240 s in the worst case, and the rest of the probe — importing aiogram, building bots,
# migrating sqlite files — has to fit in what is left. At 300 the outer bound could fire FIRST and
# replace a row-by-row report with a single "`docker run` did not finish"; the outer bound must
# never pre-empt the report it is bounding. 420 leaves ~180 s over the inner sum.
PROBE_TIMEOUT = 420

# How long a container gets to print its startup markers, and how often the log is re-read while
# waiting. Generous: the image installs nothing at start, but a cold runner reading a fresh image's
# layers off disk is slower than a warm one, and importing aiogram is not free.
BOOT_BUDGET = 90
BOOT_PAUSE = 0.5

# Large enough to hold the probe's ENTIRE report (93 lines) plus a boot log with a traceback in it.
# 4000 was not: it cut the probe transcript mid-report, taking the marker line on its last row with
# it — so the one row that says how many checks really ran became unreadable in the CI log at exactly
# the moment somebody would be looking for it.
EXCERPT_CHARS = 16000


# ══ THE PROBE ═════════════════════════════════════════════════════════════════════════════════════
# Runs inside the container, as the image's own interpreter, fed on stdin. Written as a raw string so
# that nothing in it is interpreted on the way in — it is handed to `subprocess.run(input=...)` and
# never passes through a shell, which is why it can contain quotes of both kinds without escaping.
PROBE = r'''
import ast
import asyncio
import importlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

PROBE_MARKER = "health_tracker image probe ok"

REPORT = []


def record(target, reason=None):
    REPORT.append((target, reason))


def describe(error):
    return "{}: {}".format(type(error).__name__, error)


def fail_group(prefix, error):
    """A group that could not run at all reports ONE failure rather than none.

    Silence is the dangerous outcome here: the gate counts the verdicts it receives, and a group
    that raised before recording anything would take its own targets out of the report without
    taking anything red with them.
    """
    record(prefix, "the check could not run: {}".format(describe(error)))


# -- (c) every module imports ----------------------------------------------------------------------
# Spelled out one by one rather than walked, so that a module which stops being imported by anything
# else is still checked - and so that a file deleted from src/ fails here instead of vanishing
# quietly. `main` is included because it is what the image's CMD runs.
MODULES = [
    ("main", "the module the image CMD runs"),
    ("src.settings", "the pydantic-settings model, which builds at import"),
    ("src.database", "the aiosqlite layer"),
    ("src.bot", "the aiogram handlers"),
    ("src.scheduler", "the APScheduler reminder loop"),
    ("src.utils", "the timezone parser"),
    ("src.heartbeat", "the heartbeat contract, shared by the app and the probe"),
    ("src.healthcheck", "the module the HEALTHCHECK line runs"),
    ("src.logging_setup", "the redacting stderr sink and the stdlib-logging bridge"),
]

# -- (d) the symbols the source imports, and the methods it calls on them --------------------------
# Collected from the import lines in src/ and main.py rather than from memory. This is the check that
# catches an aiogram release renaming or moving something: everything below is imported by name at
# module level, so a rename would break the bot at startup - in production, on a container that has
# already replaced the working one.
SYMBOLS = [
    ("aiogram", "Bot"),
    ("aiogram", "Dispatcher"),
    ("aiogram", "F"),
    ("aiogram", "Router"),
    ("aiogram.client.session.aiohttp", "AiohttpSession"),
    ("aiogram.client.telegram", "TelegramAPIServer"),
    ("aiogram.filters", "Command"),
    ("aiogram.fsm.context", "FSMContext"),
    ("aiogram.fsm.state", "State"),
    ("aiogram.fsm.state", "StatesGroup"),
    ("aiogram.fsm.storage.memory", "MemoryStorage"),
    ("aiogram.types", "BotCommand"),
    ("aiogram.types", "BufferedInputFile"),
    ("aiogram.types", "CallbackQuery"),
    ("aiogram.types", "InlineKeyboardButton"),
    ("aiogram.types", "InlineKeyboardMarkup"),
    ("aiogram.types", "Message"),
    ("apscheduler.schedulers.asyncio", "AsyncIOScheduler"),
    ("apscheduler.triggers.cron", "CronTrigger"),
    ("aiosqlite", "Row"),
    ("aiosqlite", "IntegrityError"),
    ("loguru", "logger"),
    ("pydantic", "AliasChoices"),
    ("pydantic", "Field"),
    ("pydantic_settings", "BaseSettings"),
    ("pydantic_settings", "SettingsConfigDict"),
]

# Methods the code calls on those objects. An import that resolves says nothing about the surface
# behind it: a release that keeps `Bot` and renames `set_my_commands` would pass every check above
# and still take the bot down on its first startup.
METHODS = [
    ("aiogram", "Bot", "set_my_commands"),
    ("aiogram", "Bot", "send_message"),
    ("aiogram", "Bot", "send_document"),
    ("aiogram", "Dispatcher", "include_router"),
    ("aiogram", "Dispatcher", "start_polling"),
    ("aiogram.fsm.context", "FSMContext", "set_state"),
    ("aiogram.fsm.context", "FSMContext", "get_state"),
    ("aiogram.fsm.context", "FSMContext", "update_data"),
    ("aiogram.fsm.context", "FSMContext", "get_data"),
    ("aiogram.fsm.context", "FSMContext", "clear"),
    ("aiogram.types", "Message", "answer"),
    ("aiogram.types", "CallbackQuery", "answer"),
]

# -- (e) the handlers that must be registered ------------------------------------------------------
# Exact sets, not counts. A count alone would be satisfied by a handler registered twice while
# another was dropped - which is precisely what a bad merge produces.
EXPECTED_MESSAGE_HANDLERS = {
    "_cmd_start", "_cmd_add", "_cmd_list", "_cmd_delete", "_cmd_track", "_cmd_edit",
    "_cmd_export", "_cmd_timezone",
    "_fsm_add_name", "_fsm_add_description", "_fsm_add_time", "_fsm_track_metric",
    "_fsm_value_input", "_fsm_edit_value",
}
EXPECTED_CALLBACK_HANDLERS = {
    "_cb_pick_metric", "_cb_record", "_cb_edit_pick", "_cb_edit_field", "_cb_edit_back",
}

# The two environment names that mean the same setting, and the value each is given so the probe can
# tell which one won when both are present.
CONVENTION_NAME = "TELEGRAM_BOT_API_SERVER"
LEGACY_NAME = "TELEGRAM_API_SERVER"
CONVENTION_URL = "http://convention.invalid:8081"
LEGACY_URL = "http://legacy.invalid:8081"

# The schema as it was BEFORE the two migrations in src/database.py: `metrics` without a description
# column, and `records` with the CHECK constraint that only allowed 0..5. This is what a production
# database created by an older image looks like, and it is the only input on which those migrations
# do anything at all.
LEGACY_SCHEMA = """
CREATE TABLE users (
    user_id    INTEGER PRIMARY KEY,
    timezone   TEXT NOT NULL DEFAULT '+03:00',
    created_at INTEGER NOT NULL
);
CREATE TABLE metrics (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    remind_time        TEXT,
    last_reminded_date TEXT,
    created_at         INTEGER NOT NULL,
    UNIQUE(user_id, name)
);
CREATE TABLE records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    metric_id   INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    value       INTEGER NOT NULL CHECK(value BETWEEN 0 AND 5),
    recorded_at INTEGER NOT NULL
);
"""


def check_modules():
    for name, what in MODULES:
        target = "{} imports ({})".format(name, what)
        try:
            importlib.import_module(name)
            record(target)
        except Exception as error:
            record(target, describe(error))


def check_symbols():
    for module_name, symbol in SYMBOLS:
        target = "{}.{} exists".format(module_name, symbol)
        try:
            module = importlib.import_module(module_name)
        except Exception as error:
            record(target, "the module does not import: {}".format(describe(error)))
            continue
        if hasattr(module, symbol):
            record(target)
        else:
            record(target, "the module imports but has no attribute {!r}: it was renamed, moved or "
                           "removed upstream, and the bot imports it at module level".format(symbol))


def check_methods():
    for module_name, symbol, method in METHODS:
        target = "{}.{}.{}() exists".format(module_name, symbol, method)
        try:
            module = importlib.import_module(module_name)
            owner = getattr(module, symbol)
        except Exception as error:
            record(target, "cannot reach {}.{}: {}".format(module_name, symbol, describe(error)))
            continue
        if hasattr(owner, method):
            record(target)
        else:
            record(target, "{} has no attribute {!r}: the call site in src/ would raise at "
                           "runtime".format(symbol, method))


def build_bot(workdir, api_server=None):
    """Construct a real HealthBot, optionally against a local Bot API server.

    src/bot.py does `from src.settings import settings`, i.e. it holds its own reference to the
    settings object - so redirecting the module-level singleton is not enough and this rebinds the
    name INSIDE src.bot. Rebuilding Settings from a patched environment is what lets one container
    exercise both branches of the endpoint decision.
    """
    import src.bot
    import src.settings
    from src.database import Database

    saved = {name: os.environ.get(name) for name in (CONVENTION_NAME, LEGACY_NAME)}
    try:
        for name in (CONVENTION_NAME, LEGACY_NAME):
            os.environ.pop(name, None)
        if api_server is not None:
            os.environ[CONVENTION_NAME] = api_server
        patched = src.settings.Settings()
        original = src.bot.settings
        src.bot.settings = patched
        try:
            db = Database(os.path.join(workdir, "bot", "health.db"))
            return src.bot.HealthBot(db)
        finally:
            src.bot.settings = original
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def check_handlers(workdir):
    """(e) Every handler registered against a REAL Dispatcher and Router.

    No mock anywhere: the object under test is aiogram's own observer, so a filter type aiogram
    stopped accepting, or a registration call whose signature changed, fails here exactly as it
    would at startup. The container has no network at all, which is what makes "constructing this
    performs no network I/O" a fact rather than a hope.
    """
    from aiogram import Dispatcher, Router
    try:
        bot = build_bot(workdir)
    except Exception as error:
        fail_group("HealthBot constructs with no network available", error)
        return
    record("HealthBot constructs with no network available (this container has --network none)")

    target = "HealthBot holds a real aiogram Dispatcher"
    if isinstance(bot.dp, Dispatcher):
        record(target)
    else:
        record(target, "bot.dp is {!r}".format(type(bot.dp)))

    routers = list(getattr(bot.dp, "sub_routers", []))
    target = "the Dispatcher has exactly one sub-router"
    if len(routers) == 1:
        record(target)
    else:
        record(target, "it has {}: {!r}".format(len(routers), routers))
        return

    router = routers[0]
    target = "the sub-router is a real aiogram Router"
    if isinstance(router, Router):
        record(target)
    else:
        record(target, "it is {!r}".format(type(router)))
        return

    for observer_name, expected in (("message", EXPECTED_MESSAGE_HANDLERS),
                                    ("callback_query", EXPECTED_CALLBACK_HANDLERS)):
        target = "the router registers exactly the {} handlers src/bot.py defines".format(
            observer_name)
        try:
            observer = getattr(router, observer_name)
            registered = set()
            for handler in observer.handlers:
                registered.add(getattr(handler.callback, "__name__", repr(handler.callback)))
        except Exception as error:
            record(target, "the registrations could not be read: {}".format(describe(error)))
            continue
        if registered == expected:
            record(target)
        else:
            record(target, "missing {} / unexpected {}".format(
                sorted(expected - registered) or "nothing", sorted(registered - expected) or
                "nothing"))


def check_api_endpoint(workdir):
    """(f) THE REGRESSION TEST for the bug this change fixes.

    Read the SESSION, not the setting. The previous code read the setting perfectly well once its
    name was right and still talked to the cloud, because it passed the value to `Bot(base_url=...)`
    - and aiogram 3 takes (token, session, default, **kwargs) and silently discards everything else.
    A check that asserted "settings.telegram_api_server == the URL" would have been green on exactly
    the code that was broken. `session.api.base` is the value that decides where a request really
    goes, so it is the value this asks about.
    """
    target = "with no server configured the bot really uses api.telegram.org"
    try:
        bot = build_bot(workdir)
        base = bot.bot.session.api.base
        if "api.telegram.org" in base:
            record(target)
        else:
            record(target, "session.api.base is {!r}".format(base))
    except Exception as error:
        fail_group(target, error)

    target = "with {} set the bot really uses that server".format(CONVENTION_NAME)
    try:
        bot = build_bot(workdir, api_server=CONVENTION_URL)
        base = bot.bot.session.api.base
        if base.startswith(CONVENTION_URL):
            record(target)
        else:
            record(target, "session.api.base is {!r}, so the configured server is NOT in use - this "
                           "is the exact failure the change was made to fix".format(base))
    except Exception as error:
        fail_group(target, error)

    target = "the file endpoint follows the configured server too"
    try:
        bot = build_bot(workdir, api_server=CONVENTION_URL)
        file_base = bot.bot.session.api.file
        if file_base.startswith(CONVENTION_URL):
            record(target)
        else:
            record(target, "session.api.file is {!r}: /export would still send its CSV through the "
                           "cloud".format(file_base))
    except Exception as error:
        fail_group(target, error)

    target = "the configured server is not treated as local-filesystem"
    try:
        bot = build_bot(workdir, api_server=CONVENTION_URL)
        if bot.bot.session.api.is_local is False:
            record(target)
        else:
            record(target, "is_local is {!r}. telegram-bot-api runs on a different host from this "
                           "bot, so file paths it returns are not readable here".format(
                               bot.bot.session.api.is_local))
    except Exception as error:
        fail_group(target, error)

    target = "the endpoint decision is logged in both branches"
    try:
        import src.bot
        source = ""
        path = getattr(src.bot, "__file__", None)
        if path:
            with open(path) as handle:
                source = handle.read()
        if "Bot API endpoint: local server" in source and "Bot API endpoint: cloud default" in source:
            record(target)
        else:
            record(target, "src/bot.py no longer carries both log lines. The whole point of the fix "
                           "is that this class of failure stops being silent")
    except Exception as error:
        fail_group(target, error)


def settings_from_env(**overrides):
    """Build a fresh Settings with the two API-server names forced to a known state."""
    import src.settings
    saved = {name: os.environ.get(name) for name in (CONVENTION_NAME, LEGACY_NAME)}
    try:
        for name in (CONVENTION_NAME, LEGACY_NAME):
            os.environ.pop(name, None)
        for name, value in overrides.items():
            os.environ[name] = value
        return src.settings.Settings()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def check_settings():
    """(j) The guard, and the two accepted names for one setting."""
    target = "a missing TELEGRAM_BOT_TOKEN fails the process with a message naming the field"
    try:
        env = dict(os.environ)
        env.pop("TELEGRAM_BOT_TOKEN", None)
        completed = subprocess.run(
            [sys.executable, "-c", "import src.settings"],
            cwd="/app", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=60, text=True)
        output = completed.stdout or ""
        if completed.returncode == 0:
            record(target, "it exited 0 and started anyway, so the bot would run token-less and "
                           "fail later, somewhere less obvious")
        elif "telegram_bot_token" not in output.lower():
            record(target, "it failed, but the output never names the field: {}".format(
                output.strip()[:400]))
        else:
            record(target)
    except Exception as error:
        fail_group(target, error)

    cases = [
        ("the park convention name {} is read".format(CONVENTION_NAME),
         {CONVENTION_NAME: CONVENTION_URL}, CONVENTION_URL),
        ("the legacy name {} is still accepted".format(LEGACY_NAME),
         {LEGACY_NAME: LEGACY_URL}, LEGACY_URL),
        ("with both set, {} wins".format(CONVENTION_NAME),
         {CONVENTION_NAME: CONVENTION_URL, LEGACY_NAME: LEGACY_URL}, CONVENTION_URL),
        ("with neither set the value is None", {}, None),
    ]
    for target, overrides, expected in cases:
        try:
            value = settings_from_env(**overrides).telegram_api_server
            if value == expected:
                record(target)
            else:
                record(target, "got {!r}, expected {!r}".format(value, expected))
        except Exception as error:
            fail_group(target, error)

    target = "a Bot API server address with no scheme is rejected, and the token is not in the error"
    # THE LEAK THIS CLOSES: aiohttp answers a schemeless URL with NonHttpUrlClientError whose text
    # is the whole request URL, and a Telegram request URL carries the token in its path. aiogram
    # wraps that in TelegramNetworkError and it reaches the log. Refusing the value at startup is
    # what keeps that request from ever being built - so this row is about a secret, not about
    # input hygiene, and it checks BOTH halves: that it is refused, and that the refusal is quiet.
    try:
        rejected = False
        message = ""
        try:
            settings_from_env(**{CONVENTION_NAME: "10.0.0.1:8081"})
        except Exception as error:
            rejected = True
            message = str(error)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        secret = token.partition(":")[2]
        if not rejected:
            record(target, "it was accepted. The first request built from it raises an error whose "
                           "text is the full URL, and the URL carries the token")
        elif token and (token in message or (secret and secret in message)):
            record(target, "it is rejected, but the error text carries the token itself")
        else:
            record(target)
    except Exception as error:
        fail_group(target, error)

    target = "the log redaction really removes a token from the text of an exception"
    try:
        from src.logging_setup import TOKEN_PLACEHOLDER, redact
        token = "123456789:AAtokenThatMustNotSurviveRedaction00"
        leaked = "NonHttpUrlClientError: http://host:8081/bot{}/getUpdates".format(token)
        scrubbed = redact(leaked, token)
        if token in scrubbed or token.partition(":")[2] in scrubbed:
            record(target, "the token is still in the redacted text, so the sink in "
                           "src/logging_setup.py would write it to stderr as it stands")
        elif TOKEN_PLACEHOLDER not in scrubbed:
            record(target, "nothing was replaced: {!r}".format(scrubbed[:200]))
        else:
            record(target)
    except Exception as error:
        fail_group(target, error)

    target = "DATABASE_PATH and the heartbeat default agree on one definition"
    try:
        from src.heartbeat import DEFAULT_DATABASE_PATH
        import src.settings
        default = src.settings.Settings.model_fields["database_path"].default
        if default == DEFAULT_DATABASE_PATH:
            record(target)
        else:
            record(target, "Settings defaults to {!r} while the probe would look next to {!r}, so "
                           "the health probe would grade a file nothing writes".format(
                               default, DEFAULT_DATABASE_PATH))
    except Exception as error:
        fail_group(target, error)


def table_sql(path, name):
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    finally:
        connection.close()
    return row[0] if row else None


def object_names(path, kind):
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,)).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def check_database_fresh(workdir):
    """(g) The schema really appears on a real file, and init() is safe to run twice."""
    from src.database import Database
    path = os.path.join(workdir, "fresh", "health.db")

    async def build():
        db = Database(path)
        await db.init()
        await db.close()
        # Twice: the `ALTER TABLE metrics ADD COLUMN description` in init() runs unconditionally and
        # raises on the second pass, where it is swallowed. If that swallow ever narrows, every
        # restart of the container would die here - on a container that had already replaced the
        # working one.
        again = Database(path)
        await again.init()
        await again.close()

    try:
        asyncio.run(build())
        record("Database.init() runs twice over the same file without raising")
    except Exception as error:
        fail_group("Database.init() runs twice over the same file without raising", error)
        return

    tables = object_names(path, "table")
    for name in ("users", "metrics", "records"):
        target = "a fresh database has the {} table".format(name)
        if name in tables:
            record(target)
        else:
            record(target, "sqlite_master holds only {}".format(sorted(tables)))

    indexes = object_names(path, "index")
    for name in ("idx_records_metric", "idx_metrics_user"):
        target = "a fresh database has the {} index".format(name)
        if name in indexes:
            record(target)
        else:
            record(target, "sqlite_master holds only {}".format(sorted(indexes)))

    target = "a fresh records table carries the -5..5 CHECK constraint"
    sql = table_sql(path, "records") or ""
    if "between -5 and 5" in sql.lower():
        record(target)
    else:
        record(target, "its definition is {!r}".format(sql))

    target = "a fresh metrics table carries the description column"
    sql = table_sql(path, "metrics") or ""
    if "description" in sql.lower():
        record(target)
    else:
        record(target, "its definition is {!r}".format(sql))


def check_database_migration(workdir):
    """(g) Both migrations run on a database shaped the way production's really is.

    A fresh file exercises neither of them: `CREATE TABLE IF NOT EXISTS` writes the new shape
    outright, so the ALTER and the records rebuild are dead code until they meet an OLD file. This
    stages one - an empty legacy schema with a single record in it - because the rebuild copies rows
    across and a migration that loses them is worse than one that fails.
    """
    from src.database import Database
    path = os.path.join(workdir, "legacy", "health.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    try:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(LEGACY_SCHEMA)
            connection.execute(
                "INSERT INTO users (user_id, timezone, created_at) VALUES (1, '+03:00', 1)")
            connection.execute(
                "INSERT INTO metrics (id, user_id, name, remind_time, last_reminded_date, "
                "created_at) VALUES (1, 1, 'mood', '09:00', NULL, 1)")
            connection.execute(
                "INSERT INTO records (user_id, metric_id, value, recorded_at) VALUES (1, 1, 3, 100)")
            connection.commit()
        finally:
            connection.close()
    except Exception as error:
        fail_group("a legacy database can be staged for the migration checks", error)
        return

    async def migrate():
        db = Database(path)
        await db.init()
        await db.close()

    try:
        asyncio.run(migrate())
        record("Database.init() migrates a legacy database without raising")
    except Exception as error:
        fail_group("Database.init() migrates a legacy database without raising", error)
        return

    target = "the ALTER TABLE migration added metrics.description"
    sql = table_sql(path, "metrics") or ""
    if "description" in sql.lower():
        record(target)
    else:
        record(target, "the legacy metrics table is still {!r}".format(sql))

    target = "the records table was rebuilt with the -5..5 CHECK constraint"
    sql = table_sql(path, "records") or ""
    if "between -5 and 5" in sql.lower():
        record(target)
    else:
        record(target, "it still reads {!r}, so a reminder answered -3 would be rejected by the "
                       "database".format(sql))

    target = "the rebuild kept the rows that were already there"
    try:
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute(
                "SELECT user_id, metric_id, value, recorded_at FROM records").fetchall()
        finally:
            connection.close()
        if rows == [(1, 1, 3, 100)]:
            record(target)
        else:
            record(target, "records now holds {!r}".format(rows))
    except Exception as error:
        fail_group(target, error)

    target = "the rebuild dropped its records_old scratch table"
    tables = object_names(path, "table")
    if "records_old" not in tables:
        record(target)
    else:
        record(target, "records_old is still there, so the next init() would migrate again")

    target = "a negative value is accepted after the migration"
    try:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "INSERT INTO records (user_id, metric_id, value, recorded_at) VALUES (1, 1, -5, 101)")
            connection.commit()
            record(target)
        finally:
            connection.close()
    except Exception as error:
        record(target, "the insert was rejected: {}".format(describe(error)))


def check_scheduler(workdir):
    """(h) Two jobs, kept apart on purpose, and the startup mark the first probe depends on.

    The SEPARATION is the property under test, not a detail of it. While the mark was written by
    check_reminders it inherited that job's schedule: APScheduler's max_instances=1 drops every
    tick that arrives while the previous execution is still running, and check_reminders awaits
    one Bot API call per due metric with a 60 s per-request timeout. A Bot API server that stopped
    answering in a minute when reminders were due therefore stopped the heartbeat, and the probe
    reported `unhealthy` on a service whose loop was turning perfectly well - which hands the
    updater a rollback of a good image. These rows are what stops the two being merged back.
    """
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    from src.heartbeat import HEARTBEAT_INTERVAL
    from src.database import Database
    from src.scheduler import ReminderScheduler, check_reminders, touch_heartbeat

    path = os.path.join(workdir, "scheduler", "health.db")
    state = {}

    async def run():
        db = Database(path)
        await db.init()
        bot = build_bot(workdir)
        scheduler = ReminderScheduler(db, bot)
        scheduler.start()
        state["jobs"] = scheduler.scheduler.get_jobs()
        state["heartbeat"] = scheduler.heartbeat_path
        state["heartbeat_exists"] = os.path.exists(scheduler.heartbeat_path)
        scheduler.stop()
        await db.close()

    try:
        asyncio.run(run())
        record("ReminderScheduler starts and stops on a real AsyncIOScheduler")
    except Exception as error:
        fail_group("ReminderScheduler starts and stops on a real AsyncIOScheduler", error)
        return

    jobs = state.get("jobs") or []
    by_callable = {}
    for job in jobs:
        by_callable[getattr(job.func, "__name__", repr(job.func))] = job

    target = "starting the scheduler registers exactly two jobs, the reminders and the heartbeat"
    if len(jobs) == 2 and set(by_callable) == {"check_reminders", "touch_heartbeat"}:
        record(target)
    else:
        record(target, "it registered {}: {!r}".format(len(jobs), sorted(by_callable)))
        return

    job = by_callable["check_reminders"]
    target = "the reminder job is check_reminders on a once-a-minute cron, with an explicit grace"
    problems = []
    if job.func is not check_reminders:
        problems.append("the callable is {!r}".format(job.func))
    if not isinstance(job.trigger, CronTrigger):
        problems.append("the trigger is {!r}".format(job.trigger))
    elif "minute='*'" not in str(job.trigger):
        problems.append("the trigger reads {!r}".format(str(job.trigger)))
    # The number itself is src/scheduler.py's business; what this forbids is falling back to
    # APScheduler's default of ONE SECOND, which drops a late tick and the reminders with it.
    grace = job.misfire_grace_time
    if grace is not None and grace < 60:
        problems.append("misfire_grace_time is {!r}, i.e. at or near the 1 s default".format(grace))
    if problems:
        record(target, "; ".join(problems))
    else:
        record(target)

    job = by_callable["touch_heartbeat"]
    target = "the heartbeat job is a separate {}s interval job that only writes the mark".format(
        HEARTBEAT_INTERVAL)
    problems = []
    if job.func is not touch_heartbeat:
        problems.append("the callable is {!r}".format(job.func))
    if not isinstance(job.trigger, IntervalTrigger):
        problems.append("the trigger is {!r}".format(job.trigger))
    elif job.trigger.interval.total_seconds() != HEARTBEAT_INTERVAL:
        problems.append("it fires every {}s, not every {}s".format(
            job.trigger.interval.total_seconds(), HEARTBEAT_INTERVAL))
    if job.max_instances != 1:
        problems.append("max_instances is {!r}".format(job.max_instances))
    if problems:
        record(target, "; ".join(problems))
    else:
        record(target)

    target = "the mark is written at startup, next to the database"
    expected = os.path.join(os.path.dirname(path), "heartbeat")
    if state.get("heartbeat") != expected:
        record(target, "the scheduler writes to {!r}, not to {!r}".format(
            state.get("heartbeat"), expected))
    elif not state.get("heartbeat_exists"):
        record(target, "start() left no file behind, so the FIRST health probe of a new container "
                       "would read a missing mark and start burning retries")
    else:
        record(target)


# Each `python -m src.healthcheck` run below gets this many seconds. It imports one stdlib-only
# module, opens one file and reads two integers out of it, so 30 s is already absurdly generous -
# and the number is not free: six of these runs are what the outer PROBE_TIMEOUT has to sit above.
HEALTHCHECK_RUN_TIMEOUT = 30
# The PID staged into a mark that is supposed to be REJECTED as inherited. Any value other than
# src.heartbeat.CONTAINER_MAIN_PID would do; a large one is used so it cannot collide with a real
# pid in this container by accident and make the row pass for the wrong reason.
FOREIGN_PID = 424242


def run_health_probe(env_overrides):
    """Run the real `python -m src.healthcheck`, the same command line the Dockerfile uses."""
    env = dict(os.environ)
    env.pop("HEARTBEAT_FILE", None)
    env.update(env_overrides)
    completed = subprocess.run(
        [sys.executable, "-m", "src.healthcheck"],
        cwd="/app", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=HEALTHCHECK_RUN_TIMEOUT, text=True)
    return completed.returncode, completed.stdout or ""


def check_health_probe(workdir):
    """(i) The probe goes round in EVERY direction, and the inherited mark is dropped.

    A probe only ever exercised on a fresh mark is indistinguishable from `exit 0` - it would gate
    nothing and would report every wedged container healthy forever. So each rejecting branch is
    staged: an aged mark, no mark, and a mark bearing another process's PID.

    That last one is the case an updater creates every single time it deploys. The mark sits on the
    data volume, the replacement container is created over that same volume, and the mark the
    outgoing container wrote seconds ago is therefore on disk before the new one has done anything.
    Two independent things reject it - main.py deletes the file at startup, and the probe refuses a
    PID that is not the container's main process - and both are checked here, one directly and one
    (the deletion) through the order in which main.py does its startup.

    This container's own interpreter is PID 1, exactly as the image's CMD is, which is what lets
    the REAL writer's output be handed to the REAL probe below without staging anything.
    """
    from src.heartbeat import (
        CONTAINER_MAIN_PID, HEARTBEAT_MAX_AGE, clear_heartbeat, format_mark, write_heartbeat)

    directory = os.path.join(workdir, "probe")
    os.makedirs(directory, exist_ok=True)
    mark = os.path.join(directory, "heartbeat")

    def stage(path, pid, timestamp):
        """Write a mark with chosen contents, through the app's own format_mark()."""
        with open(path, "w") as handle:
            handle.write(format_mark(pid, timestamp))

    target = "a fresh mark makes the probe exit 0"
    try:
        write_heartbeat(mark)
        status, output = run_health_probe({"HEARTBEAT_FILE": mark})
        if status == 0:
            record(target)
        else:
            record(target, "it exited {} on a mark the real writer had just written: {}".format(
                status, output.strip()[:400]))
    except Exception as error:
        fail_group(target, error)

    target = "a mark older than {}s makes the probe exit non-zero".format(HEARTBEAT_MAX_AGE)
    try:
        # Staged through the CONTENT, not through the mtime: the age the probe grades is the
        # timestamp the writer put in the file.
        stage(mark, CONTAINER_MAIN_PID, time.time() - (HEARTBEAT_MAX_AGE + 30))
        status, output = run_health_probe({"HEARTBEAT_FILE": mark})
        if status != 0:
            record(target)
        else:
            record(target, "it exited 0 on a mark {}s old, so the probe is green whatever happens "
                           "and gates nothing".format(int(HEARTBEAT_MAX_AGE + 30)))
    except Exception as error:
        fail_group(target, error)

    target = "a mark written by another process makes the probe exit non-zero"
    try:
        stage(mark, FOREIGN_PID, time.time())
        status, output = run_health_probe({"HEARTBEAT_FILE": mark})
        if status != 0:
            record(target)
        else:
            record(target, "it exited 0 on a mark this process did not write. That is the mark the "
                           "container being REPLACED leaves on the shared volume, seconds old - so "
                           "the first probe of a container that came up and wedged would report it "
                           "healthy, and the updater would keep the broken image")
    except Exception as error:
        fail_group(target, error)

    target = "a missing mark makes the probe exit non-zero"
    try:
        os.remove(mark)
        status, output = run_health_probe({"HEARTBEAT_FILE": mark})
        if status != 0:
            record(target)
        else:
            record(target, "it exited 0 with no mark on disk at all")
    except Exception as error:
        fail_group(target, error)

    target = "clear_heartbeat() removes an inherited mark, and tolerates there being none"
    try:
        inherited = os.path.join(directory, "inherited")
        stage(inherited, FOREIGN_PID, time.time())
        clear_heartbeat(inherited)
        if os.path.exists(inherited):
            record(target, "the file is still there, so 'no mark' would go on meaning 'the previous "
                           "container's mark' for as long as a hung startup lasted")
        else:
            # Again on a path that is not there: the normal case on a fresh volume, and it must not
            # raise - it runs before anything else in main().
            clear_heartbeat(inherited)
            record(target)
    except Exception as error:
        fail_group(target, error)

    target = "main.py clears the inherited mark before anything that can block"
    # A SOURCE-ORDER check, and it is worth being plain about how weak that is: it reads main.py
    # rather than running it, because running main() means polling Telegram. What it establishes is
    # exactly the property that matters and nothing more - that the deletion is not somewhere below
    # db.init(), which is the call that blocks while the outgoing container still holds the
    # database, i.e. the very scenario the deletion exists for.
    # Over the AST and not over the text: the first version of this check searched the source with
    # str.find() and was fooled by main.py's own COMMENT, which names db.init() several lines above
    # the call it is describing. Call nodes cannot be spelled in a comment.
    try:
        import main as main_module

        def call_name(node):
            """`clear_heartbeat` / `db.init` / `scheduler.start` - dotted, so bot.start() is not
            mistaken for scheduler.start()."""
            func = node.func
            if isinstance(func, ast.Name):
                return func.id
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                return "{}.{}".format(func.value.id, func.attr)
            return None

        with open(main_module.__file__) as handle:
            tree = ast.parse(handle.read())
        first_line = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = call_name(node)
                if name is not None and name not in first_line:
                    first_line[name] = node.lineno
        clear_at = first_line.get("clear_heartbeat")
        init_at = first_line.get("db.init")
        start_at = first_line.get("scheduler.start")
        if clear_at is None:
            record(target, "main.py never calls clear_heartbeat(), so a mark left by the container "
                           "this one replaced is what the first probes read")
        elif init_at is None or start_at is None:
            record(target, "main.py no longer calls db.init() or scheduler.start(), so this ordering "
                           "check has nothing to compare against and proves nothing as written")
        elif clear_at < init_at and clear_at < start_at:
            record(target)
        else:
            record(target, "it is called after db.init() / scheduler.start(), so a startup that "
                           "hangs in either still leaves the previous container's mark in place")
    except Exception as error:
        fail_group(target, error)

    # With no HEARTBEAT_FILE the probe has to find the mark on its own, from DATABASE_PATH. That
    # resolution is the one production uses - the override exists for this gate - so it is checked
    # in both directions too: present and readable, then removed.
    default_dir = os.path.join(workdir, "defaulted")
    os.makedirs(default_dir, exist_ok=True)
    default_db = os.path.join(default_dir, "health.db")
    default_mark = os.path.join(default_dir, "heartbeat")

    target = "with no override the probe finds the mark next to DATABASE_PATH"
    try:
        write_heartbeat(default_mark)
        status, output = run_health_probe({"DATABASE_PATH": default_db})
        if status == 0:
            record(target)
        else:
            record(target, "it exited {}: {}".format(status, output.strip()[:400]))
    except Exception as error:
        fail_group(target, error)

    target = "and it is really THAT file the probe reads"
    try:
        os.remove(default_mark)
        status, output = run_health_probe({"DATABASE_PATH": default_db})
        if status != 0:
            record(target)
        else:
            record(target, "removing the mark changed nothing, so the probe is reading some other "
                           "file - or nothing at all")
    except Exception as error:
        fail_group(target, error)


def main():
    workdir = tempfile.mkdtemp(prefix="smoke-")
    check_modules()
    check_symbols()
    check_methods()
    check_handlers(workdir)
    check_api_endpoint(workdir)
    check_database_fresh(workdir)
    check_database_migration(workdir)
    check_scheduler(workdir)
    check_health_probe(workdir)
    check_settings()

    failed = 0
    for target, reason in REPORT:
        if reason is None:
            print("ok   {}".format(target))
        else:
            print("FAIL {} -> {}".format(target, reason))
            failed += 1
    # The marker line carries the count so the outer half can tell a probe that ran to its end from
    # one that stopped emitting rows halfway and still exited 0.
    print("{}: {}/{} targets".format(PROBE_MARKER, len(REPORT), len(REPORT)))
    # SystemExit, never assert: asserts vanish under PYTHONOPTIMIZE and would take this whole gate
    # green with them.
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
'''


# ══ THE OUTER HALF ════════════════════════════════════════════════════════════════════════════════

def excerpt(text):
    """Bound what reaches the log, and say so when something was cut."""
    if text is None:
        return ""
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS] + "\n[... truncated at {} characters]".format(EXCERPT_CHARS)


def docker(args, timeout, stdin_text=None):
    """Run a docker command.

    Returns (status, output) with stderr folded into stdout, because everything here is read by a
    human out of a CI log where the interleaving is the useful part. A status of None means the
    command produced no exit code at all — it timed out, or docker is not there — and `output` then
    explains which. Callers must keep that case apart from a non-zero exit: they mean different
    things and only one of them is a verdict about the image.
    """
    argv = ["docker"] + args
    try:
        completed = subprocess.run(
            argv,
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True)
    except FileNotFoundError:
        return None, (
            "`docker` is not on PATH. This gate drives the daemon from the runner, so it cannot run "
            "anywhere the docker CLI is missing")
    except subprocess.TimeoutExpired as error:
        return None, "`{}` did not finish within {} s. Output so far:\n{}".format(
            " ".join(argv), timeout, excerpt(error.output))
    return completed.returncode, completed.stdout or ""


def remove_container(name):
    """Best effort. Never the reason a check fails; the workflow cleans up too."""
    docker(["rm", "-f", name], REMOVE_TIMEOUT)


def probe_report_rows(output, prefix):
    """Parse the probe's own report lines back into rows of this gate's report.

    The probe prints the same `ok   <target>` / `FAIL <target> -> <reason>` shape this file does, so
    its verdicts merge into one list instead of arriving as a single opaque "the probe failed". That
    matters for the same reason every other check here has one row per claim: a run that breaks four
    things should say four things.
    """
    rows = []
    for line in output.splitlines():
        if line.startswith("ok   "):
            rows.append((prefix + line[5:], None))
        elif line.startswith("FAIL "):
            target, _, reason = line[5:].partition(" -> ")
            rows.append((prefix + target, reason or "the probe reported FAIL with no reason"))
    return rows


def wait_for_markers(name, markers, budget):
    """Poll `docker logs` until EVERY marker has appeared, or the budget runs out.

    All of them, not the first. Returning on whichever arrived first would leave the later ones
    judged against a snapshot taken BEFORE they could have been written — a check that reports the
    absence of something it never gave a chance to appear. Bounded in WALL CLOCK rather than in
    attempts, because each attempt shells out to `docker logs` with its own bound and an
    attempt-counted loop would multiply into minutes on a slow daemon.

    Returns the logs of the last poll.
    """
    deadline = time.monotonic() + budget
    logs = ""
    while True:
        status, logs = docker(["logs", name], LOGS_TIMEOUT)
        if status == 0 and all(marker in logs for marker in markers):
            return logs
        if time.monotonic() >= deadline:
            return logs
        time.sleep(BOOT_PAUSE)


def check_image_contract(image):
    """(a) What the image DECLARES: its command, where it runs, and the probe with its timings.

    The healthcheck rows carry the weight. Everything else in this file could pass on an image whose
    HEALTHCHECK had been deleted or slowed down — and the consequence of that would not show up in
    any run of this gate at all: it shows up months later, as an automatic update that rolled itself
    back because `healthy` never arrived inside the updater's window.
    """
    status, output = docker(["inspect", "--format", "{{json .Config}}", image], INSPECT_TIMEOUT)
    if status != 0:
        return [("the image declares its runtime contract",
                 "`docker inspect` on {} returned {}: {}".format(image, status, excerpt(output)))]
    try:
        config = json.loads(output)
    except ValueError as error:
        return [("the image declares its runtime contract",
                 "`docker inspect` produced no usable JSON ({}): {}".format(error,
                                                                            excerpt(output)))]

    rows = []

    target = "the image declares CMD {}".format(EXPECTED_CMD)
    actual = config.get("Cmd")
    rows.append((target, None) if actual == EXPECTED_CMD else (target, "it declares {!r}".format(
        actual)))

    target = "the image declares WORKDIR {}".format(APP_DIR)
    actual = config.get("WorkingDir")
    rows.append((target, None) if actual == APP_DIR else (target, "it declares {!r}".format(actual)))

    health = config.get("Healthcheck")
    target = "the image declares a HEALTHCHECK at all"
    if not health:
        rows.append((target, (
            "it declares none. The container carries io.portainer.update.enable, and the updater's "
            "rollback of a bad image is triggered by the new container failing to become healthy — "
            "with no healthcheck there is nothing to become, and a broken update stays deployed")))
        return rows
    rows.append((target, None))

    for target, actual, expected in (
            ("the HEALTHCHECK runs {}".format(EXPECTED_HEALTHCHECK_TEST[-1]),
             health.get("Test"), EXPECTED_HEALTHCHECK_TEST),
            ("the HEALTHCHECK interval is 30s", health.get("Interval"),
             EXPECTED_HEALTHCHECK_INTERVAL),
            ("the HEALTHCHECK timeout is 5s", health.get("Timeout"),
             EXPECTED_HEALTHCHECK_TIMEOUT),
            ("the HEALTHCHECK start period is 30s", health.get("StartPeriod"),
             EXPECTED_HEALTHCHECK_START_PERIOD),
            ("the HEALTHCHECK allows 3 retries", health.get("Retries"),
             EXPECTED_HEALTHCHECK_RETRIES)):
        if actual == expected:
            rows.append((target, None))
        else:
            rows.append((target, (
                "it is {!r}, expected {!r}. These numbers decide, against the ~120 s the Portainer "
                "updater waits before rolling the image back, both how fast a good container can "
                "report healthy (the first probe lands one --start-interval in, ~5 s) and how long "
                "a bad one takes to be graded unhealthy (--start-period + --retries x --interval = "
                "120 s, because failures inside the start period do not count towards the "
                "retries)".format(actual, expected))))
    return rows


def step_script(path, step_name):
    """Return the executable lines of the `run:` body of the named step of a workflow file.

    Hand-rolled rather than parsed as YAML: pyyaml is not in the runner's job image and this gate
    installs nothing. What is needed is narrow enough to do by indentation — find `- name: <step>`,
    find the `run: |` under it, and take the block indented deeper than that key.

    COMMENTS AND BLANK LINES ARE DROPPED, and the remaining lines are dedented by the block's own
    indentation. The two workflows are NOT byte-identical and are not meant to be — each explains
    itself in its own words, and the publishing one has more to explain. What must not differ is
    what actually runs.

    Raises ValueError when the step or its body cannot be found, so that a comparison which cannot
    be made is reported as a failure rather than passing on two empty lists.
    """
    with open(path) as handle:
        lines = handle.read().splitlines()

    start = None
    for index, line in enumerate(lines):
        if line.strip() == "- name: {}".format(step_name):
            start = index
            break
    if start is None:
        raise ValueError("{} has no step named {!r}".format(os.path.basename(path), step_name))

    run_at = None
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        # The next step begins: this one had no `run:` of its own (a `uses:` step, say).
        if stripped.startswith("- name:"):
            break
        if stripped in ("run: |", "run: |-"):
            run_at = index
            break
    if run_at is None:
        raise ValueError("the {!r} step of {} has no literal `run: |` body".format(
            step_name, os.path.basename(path)))

    run_indent = len(lines[run_at]) - len(lines[run_at].lstrip())
    body = []
    for line in lines[run_at + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= run_indent:
            break
        body.append(line)

    if not body:
        raise ValueError("the {!r} step of {} has an empty `run:` body".format(
            step_name, os.path.basename(path)))

    dedent = min(len(line) - len(line.lstrip()) for line in body)
    return [line[dedent:].rstrip() for line in body if not line.strip().startswith("#")]


def check_workflow_parity():
    """(k) The steps the two workflows claim to share really do run the same lines.

    Both files say so in a comment, and a comment is exactly as strong as nobody's attention. The
    drift this catches is one-directional and quiet: the publishing workflow is the one people edit
    (it is the one that ships), so what rots is the PR gate — which then goes on being green while
    testing less than the thing it is rehearsing for.

    Read out of the CHECKOUT, from paths derived from this file's own location rather than from the
    working directory, because the workflows invoke this script as `python3 ci/smoke.py` and a
    relative path would depend on where that was run from.
    """
    rows = []
    for step_name in DUPLICATED_STEPS:
        target = "both workflows run the same lines in the {!r} step".format(step_name)
        try:
            publish = step_script(PUBLISH_WORKFLOW, step_name)
            tests = step_script(TESTS_WORKFLOW, step_name)
        except (OSError, ValueError) as error:
            rows.append((target, "the comparison could not be made: {}".format(error)))
            continue
        if publish == tests:
            rows.append((target, None))
        else:
            only_publish = [line for line in publish if line not in tests]
            only_tests = [line for line in tests if line not in publish]
            rows.append((target, (
                "they have drifted. Only in image-check-publish.yml: {!r}; only in tests.yml: "
                "{!r}".format(only_publish[:5], only_tests[:5]))))
    return rows


def start_boot_container(image, name, extra_env):
    """Start the image the way production does: the real CMD, no override.

    `--network none` so the fake token below can never reach Telegram and so the container fails
    fast instead of retrying against the internet from a shared runner. It WILL die shortly after
    the lines this gate is reading — the first API call has nowhere to go — and that is expected:
    what is being checked is that startup reaches its first log lines and writes its mark, not that
    a bot with a made-up token stays up.
    """
    remove_container(name)
    args = ["run", "-d", "--name", name, "--network", "none",
            "-e", "TELEGRAM_BOT_TOKEN=" + FAKE_TOKEN]
    for key, value in extra_env.items():
        args += ["-e", "{}={}".format(key, value)]
    args.append(image)
    status, output = docker(args, START_TIMEOUT)
    if status != 0:
        return [("the container starts from the image's own CMD ({})".format(name),
                 "`docker run` returned {}: {}".format(status, excerpt(output)))], False
    return [("the container starts from the image's own CMD ({})".format(name), None)], True


def check_boot_cloud(image, name):
    """(b) The default branch: no Bot API server configured.

    Six rows: the container started, three log lines, the heartbeat file, and the token check. The
    log rows prove the process got past import, settings, the database and the scheduler; the file
    row proves the startup heartbeat this image's healthcheck depends on is really written, at the
    path the probe really looks at, by the real command, with that command's PID in it — none of
    which the in-image probe can establish, because it never runs the CMD. This is the ONLY
    container whose heartbeat file is read; check_boot_local exercises the other branch of the
    endpoint decision, and the write below it is the same code either way.
    """
    rows, started = start_boot_container(image, name, {})
    if not started:
        return rows

    logs = wait_for_markers(name, (STARTUP_MARKER, CLOUD_ENDPOINT_MARKER, SCHEDULER_MARKER),
                            BOOT_BUDGET)

    for marker, what in ((STARTUP_MARKER, "the first line main.py logs"),
                         (CLOUD_ENDPOINT_MARKER,
                          "the endpoint line, cloud branch — the line that makes a misconfigured "
                          "Bot API server visible instead of silent"),
                         (SCHEDULER_MARKER,
                          "the scheduler line, which also means the startup heartbeat write below "
                          "it was reached")):
        target = "the log carries {!r} ({})".format(marker, what)
        rows.append((target, None) if marker in logs else (target, (
            "it never appeared within {} s. Log:\n{}".format(BOOT_BUDGET, excerpt(logs)))))

    target = "the startup heartbeat really exists at {}".format(HEARTBEAT_IN_IMAGE)
    # `docker cp` rather than a bind mount: it streams the file over the API, so it works from a job
    # container whose filesystem the daemon cannot see — and it works on a container that has already
    # exited, which this one may well have.
    # Into a temporary directory, not next to this script: the checkout is what the workflow archives
    # and a copy left behind by a failed read would be a stray file in it.
    scratch = tempfile.mkdtemp(prefix="smoke-cp-")
    local = os.path.join(scratch, "heartbeat")
    status, output = docker(["cp", "{}:{}".format(name, HEARTBEAT_IN_IMAGE), local], COPY_TIMEOUT)
    if status != 0:
        rows.append((target, (
            "`docker cp` returned {}: {}. The first health probe of a fresh container would read a "
            "missing mark".format(status, excerpt(output)))))
    else:
        try:
            with open(local) as handle:
                content = handle.read().strip()
        except OSError as error:
            content = "unreadable: {}".format(error)
        # The format is defined in src/heartbeat.py (format_mark): the writer's PID, a space, the
        # unix time. It is parsed by hand here rather than imported, because this half of the gate
        # deliberately knows the image only from the outside. The PID has to be 1: the image's CMD
        # is in exec form, so the interpreter IS the container's main process, and that is the
        # number src/healthcheck.py compares against before it grades the age.
        parts = content.split()
        if (len(parts) == 2 and parts[0] == "1" and parts[1].isdigit()
                and int(parts[1]) > 1700000000):
            rows.append((target, None))
        else:
            rows.append((target, (
                "it holds {!r}, which is not '<pid of the main process> <unix time>'. The probe "
                "rejects a mark it cannot parse or that names another process, so this container "
                "would never report healthy".format(content))))
    shutil.rmtree(scratch, ignore_errors=True)

    rows.append(check_token_absent(name))
    return rows


def check_boot_local(image, name):
    """(b) The other branch: TELEGRAM_BOT_API_SERVER set, exactly as the stack passes it.

    This is the end-to-end half of the endpoint regression test. The probe checks that the SESSION
    points at the configured server; this checks that the variable production really sets, spelled
    the way production really spells it, reaches that decision in the real image — and that the log
    says so, with the address in it.
    """
    rows, started = start_boot_container(image, name,
                                         {"TELEGRAM_BOT_API_SERVER": LOCAL_API_SERVER})
    if not started:
        return rows

    logs = wait_for_markers(name, (LOCAL_ENDPOINT_MARKER,), BOOT_BUDGET)

    target = "TELEGRAM_BOT_API_SERVER reaches the bot: the log names the local server"
    if LOCAL_ENDPOINT_MARKER in logs:
        rows.append((target, None))
    else:
        rows.append((target, (
            "{!r} never appeared within {} s — the name the stack passes is being dropped again. "
            "Log:\n{}".format(LOCAL_ENDPOINT_MARKER, BOOT_BUDGET, excerpt(logs)))))

    target = "and it does NOT fall back to the cloud endpoint"
    if CLOUD_ENDPOINT_MARKER not in logs:
        rows.append((target, None))
    else:
        rows.append((target, "the log carries the cloud line as well, so the branch was not taken"))

    rows.append(check_token_absent(name))
    return rows


def check_token_absent(name):
    """The token must never reach the log — not in the endpoint line, and not in a traceback.

    THE LOG IS RE-READ HERE, on purpose, instead of being handed the snapshot the marker wait
    returned. That snapshot is taken the moment the last marker appears, i.e. in the container's
    first seconds — before the bot has tried its first API call and therefore before any traceback
    exists. Judging "no token in the traceback" against a log that predates the traceback is a
    check that asks nothing, and this one is guarding a secret.

    An EMPTY log fails. A container that printed nothing at all cannot support a verdict about
    what it printed, and reporting ok on it is the same fault in a different disguise: it is
    exactly what a `docker logs` that failed, or a name that matched no container, produces.

    What it can and cannot see: src/logging_setup.py scrubs the token out of everything the
    process writes through loguru, so a leak on that path is fixed rather than caught here. This
    row is what remains — the paths that go around the sink entirely, and the case where the
    scrubbing itself stops working.

    The failure message deliberately does not repeat the value it found: a gate that printed the
    secret it caught being printed would put it in a CI log that outlives the run.
    """
    target = "the bot token never appears in the log of {}".format(name)
    status, logs = docker(["logs", name], LOGS_TIMEOUT)
    if status != 0:
        return (target, "`docker logs` returned {}: {}".format(status, excerpt(logs)))
    if not logs.strip():
        return (target, (
            "the container's log is empty, so there is nothing to clear the token of. Either it "
            "printed nothing at all or this is not the container that ran"))
    if FAKE_TOKEN in logs or FAKE_TOKEN.split(":")[1] in logs:
        return (target, "it does. In production that is a real token in a log an updater keeps")
    return (target, None)


def run_probe(image, name):
    """Feed the probe to `docker run -i --rm ... python -u -` and merge its verdicts into ours.

    `-i` attaches stdin without asking for a tty (none is needed and none is available on a runner),
    the image's own interpreter runs the program, and `docker run` propagates the command's exit
    status — which is what lets the two consistency rows below tell "the probe reported failures"
    apart from "the probe died before it could report anything".

    The program goes in on STDIN and not as a file, because there is no shared filesystem between
    this job and the daemon to put a file on. It also means the program never passes through a shell:
    no quoting, no escaping, and no way for a stray apostrophe to truncate the script into something
    that runs, checks nothing and exits 0.
    """
    remove_container(name)
    args = ["run", "-i", "--rm", "--name", name, "--network", "none",
            "-e", "TELEGRAM_BOT_TOKEN=" + FAKE_TOKEN,
            image, "python", "-u", "-"]
    status, output = docker(args, PROBE_TIMEOUT, stdin_text=PROBE)

    exit_target = "the probe's exit status agrees with its own report"
    marker_target = "the probe ran to its end, reporting all {} targets".format(
        EXPECTED_PROBE_TARGETS)

    if status is None:
        return [("the in-image probe", output)], output

    rows = probe_report_rows(output, PROBE_ROW_PREFIX)
    if not rows:
        # Exit status alone cannot be read here: a probe that printed nothing has told us nothing,
        # whatever it exited with. This is what a `docker run` that could not start the interpreter
        # looks like, and what a truncated stdin looks like.
        return [("the in-image probe", (
            "it produced no report lines at all, so none of its checks ran. `docker run` exited {}. "
            "Output:\n{}".format(status, excerpt(output))))], output

    reported_failures = any(reason is not None for _, reason in rows)
    if status != 0 and not reported_failures:
        rows.append((exit_target, (
            "`docker run` exited {} but every line the probe printed says ok — so it died after "
            "reporting and before finishing, and the report above is incomplete".format(status))))
    elif status == 0 and reported_failures:
        rows.append((exit_target, (
            "the probe printed FAIL lines yet exited 0. Its failures are supposed to leave through "
            "SystemExit(1); an exit of 0 here means they no longer do, and this gate would have gone "
            "green on them")))
    else:
        rows.append((exit_target, None))

    expected_line = "{}: {}/{} targets".format(
        PROBE_MARKER, EXPECTED_PROBE_TARGETS, EXPECTED_PROBE_TARGETS)
    if expected_line in output:
        rows.append((marker_target, None))
    elif PROBE_MARKER not in output:
        rows.append((marker_target, (
            "it never printed {!r}. The marker is on the probe's last line, so its absence means the "
            "program did not run to the end — a truncated stdin, or something that killed the "
            "interpreter mid-report".format(PROBE_MARKER))))
    else:
        # It finished, but with a different number of verdicts than this gate expects. That is a gate
        # that has quietly started proving less, which is the failure mode nothing else here can see:
        # every row it DID print says ok, and the exit status is 0.
        summary = [line for line in output.splitlines() if line.startswith(PROBE_MARKER)]
        rows.append((marker_target, (
            "it ran to the end but reported {!r} instead of {} targets. Either a check stopped "
            "emitting rows — in which case this gate is now proving less than it says it does and "
            "nothing else would have noticed — or one was added and EXPECTED_PROBE_TARGETS in this "
            "file needs updating".format(
                summary[0] if summary else "(unparseable)", EXPECTED_PROBE_TARGETS))))
    return rows, output


def main():
    image = os.environ.get(IMAGE_ENV)
    if not image:
        print("{} is not set: this gate tests the image it is given and has no default, because a "
              "default would silently gate whichever image happened to be on the daemon".format(
                  IMAGE_ENV))
        raise SystemExit(1)
    name = os.environ.get(NAME_ENV)
    if not name:
        print("{} is not set: this gate names every container it starts after it, and a default "
              "would both hide containers from the workflow's cleanup step and make two concurrent "
              "runs collide on one name".format(NAME_ENV))
        raise SystemExit(1)

    rows = []
    transcripts = []

    try:
        rows.extend(check_image_contract(image))
        # Before any container: it reads two files out of the checkout and cannot fail slowly, and
        # its verdict is about this gate's own integrity rather than about the image.
        rows.extend(check_workflow_parity())

        probe_rows, probe_output = run_probe(image, name + PROBE_SUFFIX)
        transcripts.append(("the probe, from inside the image", probe_output))
        rows.extend(probe_rows)

        rows.extend(check_boot_cloud(image, name + BOOT_CLOUD_SUFFIX))
        rows.extend(check_boot_local(image, name + BOOT_LOCAL_SUFFIX))
    finally:
        # Unconditionally. When a bound fires inside docker() it kills the docker CLIENT on the
        # runner, not the container on the daemon, so a hung run would otherwise sit here pinning the
        # image until somebody noticed. The workflow removes the same names again under
        # `if: always()`, which covers this whole script being killed by the step timeout.
        for suffix in ALL_SUFFIXES:
            remove_container(name + suffix)

    # The transcripts first, the verdicts last: in a CI log the verdicts are what somebody scrolls to
    # the bottom for, and the container's own output is what turns a one-line verdict into a
    # diagnosis.
    for label, text in transcripts:
        print("")
        print("--- {} ---".format(label))
        print(excerpt(text).rstrip() or "(no output)")
    print("")
    print("--- results ---")

    # Before a single verdict is printed: the NUMBER of verdicts is itself one. A check_* that
    # stopped emitting rows takes its own verdicts out of the report and takes nothing red with them,
    # so the run would end `smoke ok: 112/112` — three short of the 115 below, and indistinguishable
    # from a good run to anybody not counting — exit 0, and have every printed row saying ok with
    # several claims silently no longer made.
    #
    # ONLY on a run where nothing else failed, and that is not laziness about the arithmetic. A
    # failing check legitimately reports fewer rows than its happy path — run_probe emits 1 instead
    # of the 95 (93 from the probe plus its 2 consistency rows) a green run produces, when its
    # container never started — so on an already-red run this row would fire too, on top of the real
    # failure, and read as though the GATE were broken. The fault it exists to catch is invisible on
    # a red run and decisive on a green one, which is exactly where it is reported.
    if len(rows) != EXPECTED_TOTAL_TARGETS and not any(reason is not None for _, reason in rows):
        rows.append((
            "this gate produced all {} of the verdicts it is supposed to".format(
                EXPECTED_TOTAL_TARGETS),
            "it produced {}, and every one of them says ok. Either a check stopped emitting rows — "
            "in which case this gate is now proving less than it says it does and nothing else here "
            "would have noticed — or one was added or removed and EXPECTED_TOTAL_TARGETS needs "
            "updating".format(len(rows))))

    failures = []
    for target, reason in rows:
        if reason is None:
            print("ok   {}".format(target))
        else:
            print("FAIL {} -> {}".format(target, reason))
            failures.append(target)

    if failures:
        print("")
        print("smoke FAILED: {}/{} targets broken:".format(len(failures), len(rows)))
        for target in failures:
            print("  - {}".format(target))
        raise SystemExit(1)

    print("")
    print("smoke ok: {}/{} targets".format(len(rows), len(rows)))


if __name__ == "__main__":
    main()
