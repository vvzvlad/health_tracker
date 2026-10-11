from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.logging_setup import LOG_LEVELS


class Settings(BaseSettings):
    telegram_bot_token: str
    # Read from TELEGRAM_BOT_API_SERVER, the env name every bot in the park uses for the local
    # Bot API server. Note `extra="ignore"` below: a misspelled variable is dropped silently.
    telegram_bot_api_server: str | None = None
    # Nothing else derives anything from it (the heartbeat mark does not live near the database).
    database_path: str = "data/health.db"
    default_timezone: str = "+03:00"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("telegram_bot_api_server")
    @classmethod
    def _normalise_api_server(cls, value: str | None) -> str | None:
        """Treat a blank value as "not set", and reject an address with no http(s) scheme.

        A blank value (`TELEGRAM_BOT_API_SERVER=`, `${VAR:-}`, an empty Portainer field) reaches
        here as `""` (`env_ignore_empty` is False), and failing the scheme check on it would crash
        at `import src.settings`, before logging is configured, on every restart. Blank means "no
        local server". The value is returned stripped, since stray whitespace is invisible in logs.

        The scheme check is a secrecy guard: for `10.0.0.1:8081` aiohttp raises
        NonHttpUrlClientError quoting the whole request URL, whose path carries the token, and it
        would end up in `docker logs`. The message names only the variable and the fix; pydantic
        appends the rejected value, which is the server address and not a secret.

        There is deliberately no validator on telegram_bot_token: pydantic would render a
        rejected token into the error as `input_value=...`, unformatted and before the redacting
        sink exists. aiogram's `validate_token` already rejects a malformed or empty token at
        `Bot(...)`, readably and after logging is set up; main.py's `try`/`finally` makes that
        kill the container (ci/smoke.py has a row checking that an empty token dies).
        """
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        scheme = urlsplit(value).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                "the Bot API server address (TELEGRAM_BOT_API_SERVER) "
                "must start with http:// or https:// — a bare host:port cannot be turned into "
                "a request URL, and the library error that would follow quotes the full URL, "
                "which carries the bot token"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        """Upper-case the level, treat a blank value as "not set", and reject an unknown name.

        loguru's level names are upper case and its lookup is exact (loguru 0.7.2 raises
        `ValueError: Level 'info' does not exist`), and configure_logging() runs before the sink
        and excepthook exist, so `LOG_LEVEL=info` would be an unformatted crash on every restart.

        A blank value (`LOG_LEVEL=` in compose, an empty Portainer field) is an empty string to
        pydantic-settings, so it falls back to the default here.

        An unknown name is rejected here, where the error names the field and the accepted
        spellings. A log level is not a secret, so pydantic quoting `input_value=...` is fine.
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
