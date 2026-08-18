from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.heartbeat import DEFAULT_DATABASE_PATH


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
    # Imported rather than spelled out: src/heartbeat.py owns this default so the health
    # probe can derive the heartbeat path from the same value without importing Settings.
    database_path: str = DEFAULT_DATABASE_PATH
    default_timezone: str = "+03:00"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("telegram_api_server")
    @classmethod
    def _require_http_scheme(cls, value: str | None) -> str | None:
        """Reject a server address with no http(s) scheme, HERE, before a request is built.

        This is a secrecy guard, not a tidiness one. Leaving the scheme off — `10.0.0.1:8081`
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
        """
        if value is None:
            return value
        scheme = urlsplit(value).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                "the Bot API server address (TELEGRAM_BOT_API_SERVER, or TELEGRAM_API_SERVER) "
                "must start with http:// or https:// — a bare host:port cannot be turned into "
                "a request URL, and the library error that would follow quotes the full URL, "
                "which carries the bot token"
            )
        return value


settings = Settings()
