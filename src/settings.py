from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.logging_setup import LOG_LEVELS


class Settings(BaseSettings):
    telegram_bot_token: str
    # ONE setting, TWO accepted environment names, and the order is the whole point.
    #
    # TELEGRAM_BOT_API_SERVER is the name the `telegram_bots` stack really passes this
    # container, and the name the neighbouring medical_bot in the same stack receives — the
    # compose spelling is the park's convention. This field used to be declared as plain
    # `telegram_api_server`, i.e. it read TELEGRAM_API_SERVER, and the divergence was on
    # this side. With `extra="ignore"` below the mismatch cost nothing loudly: the variable
    # the stack passed was dropped without a word, the field stayed None, and the bot went
    # on talking to api.telegram.org while the line in the compose file looked like it was
    # working.
    #
    # TELEGRAM_API_SERVER stays accepted, SECOND, so that a local .env written against the
    # old name keeps working; it is consulted only when the convention name is absent.
    telegram_api_server: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TELEGRAM_BOT_API_SERVER", "TELEGRAM_API_SERVER"),
    )
    # Spelled out here, and nothing else derives anything from it. It used to live in
    # src/heartbeat.py so the health probe could place the mark beside the database without
    # importing Settings; the mark no longer goes anywhere near the database, so the reason is
    # gone and the constant is back where it is used.
    database_path: str = "data/health.db"
    default_timezone: str = "+03:00"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("telegram_api_server")
    @classmethod
    def _normalise_api_server(cls, value: str | None) -> str | None:
        """Treat a blank value as "not set", and reject an address with no http(s) scheme.

        THE BLANK CASE IS NOT COSMETIC. `TELEGRAM_BOT_API_SERVER=` in a compose file, a
        `${VAR:-}` that expanded to nothing, an environment field left empty in Portainer — all
        of them set the variable to an empty STRING, and pydantic-settings does not treat that
        as absent (`env_ignore_empty` is False by default). It reaches this validator as `""`,
        whose `urlsplit("").scheme` is `""`, so without this branch it would fail the scheme
        check below and raise a ValidationError at `import src.settings` — before
        configure_logging() has run, i.e. as an unformatted crash, on every restart, forever.
        A variable that is declared and left empty means "no local server"; the cloud branch is
        the answer, not a restart loop.

        The value is also returned STRIPPED. A trailing space or newline picked up from a
        compose file is invisible in the log line that echoes the address back and produces a
        request URL nobody can spot the fault in.

        THE REST IS A SECRECY GUARD, not a tidiness one. Leaving the scheme off — `10.0.0.1:8081`
        instead of `http://10.0.0.1:8081` — is the natural way to write this variable wrong,
        and aiohttp answers it with NonHttpUrlClientError whose text is the WHOLE request URL.
        A Telegram request URL carries the token in its path, so that exception text is the
        token; aiogram wraps it in TelegramNetworkError, something logs it, and it stays in
        `docker logs` for the life of the container. Failing at startup instead costs a
        restart loop that names the real problem.

        The message below deliberately names the variables and the fix and NOTHING else — in
        particular not the token, which is a field of this same model. pydantic appends the
        rejected value to it, and that value is the server address, which is not a secret.
        BOTH accepted spellings are named because pydantic heads the error with the first
        alias whichever one was actually set, and a message naming only that one would send
        somebody who wrote TELEGRAM_API_SERVER looking for a variable they never set.

        AND THAT IS WHY THERE IS NO VALIDATOR ON telegram_bot_token, tempting as one looks
        sitting next to this. pydantic renders a rejected value into the error text as
        `input_value=...`, so a token-format check would print the token itself — unformatted,
        before configure_logging() exists to scrub it, into a restart loop that repeats it for
        as long as the container is redeployed. It would buy little: aiogram's own
        `validate_token` rejects a malformed token at `Bot(...)` with "Token is invalid!", after
        logging is configured and without quoting the value.
        """
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        scheme = urlsplit(value).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                "the Bot API server address (TELEGRAM_BOT_API_SERVER, or TELEGRAM_API_SERVER) "
                "must start with http:// or https:// — a bare host:port cannot be turned into "
                "a request URL, and the library error that would follow quotes the full URL, "
                "which carries the bot token"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        """Upper-case the level, treat a blank value as "not set", and reject an unknown name.

        THE CASE IS THE WHOLE POINT, and it is the same class of failure as the empty server
        address above. loguru's level names are upper case and its lookup is exact: on the pinned
        loguru 0.7.2 both `logger.add(sink, level="info")` and `logger.level("info")` raise
        `ValueError: Level 'info' does not exist`. This field was a plain `str` with no
        normalisation, so `LOG_LEVEL=info` in a stack — which is how most people write it —
        raised inside configure_logging(), i.e. BEFORE the sink and the excepthook it installs
        existed: an unformatted crash, on every restart, forever.

        A blank value falls back to the default for the same reason the server address does:
        `LOG_LEVEL=` in a compose file, or an empty field in Portainer, sets an empty STRING, and
        pydantic-settings does not treat that as absent.

        An unknown name is REJECTED HERE rather than left to blow up later. Both are a startup
        failure, but this one names the field and the accepted spellings, and it arrives as a
        pydantic error rather than as a bare ValueError from inside the logging setup.

        Unlike the token, the value is safe to quote: a log level is not a secret, so pydantic
        appending `input_value=...` to the message is help rather than a leak. That asymmetry is
        exactly why telegram_bot_token still has no validator — see the note above.
        """
        value = value.strip().upper()
        if not value:
            return "INFO"
        if value not in LOG_LEVELS:
            raise ValueError(
                "LOG_LEVEL must be one of {} (case does not matter, the value is upper-cased "
                "here); loguru rejects anything else, and it does so inside the logging setup, "
                "before there is a sink to report it through".format(", ".join(LOG_LEVELS))
            )
        return value


settings = Settings()
