from pydantic import AliasChoices, Field
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


settings = Settings()
