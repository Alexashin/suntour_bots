from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv_ints(value: str) -> list[int]:
    if not value.strip():
        return []
    return [int(item.strip()) for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://suntour:suntour@db:5432/suntour"

    telegram_token: str = ""
    telegram_admin_ids: str = ""
    telegram_enable_polling: bool = True

    max_token: str = ""
    max_api_base: str = "https://platform-api2.max.ru"
    max_mode: Literal["polling", "webhook"] = "polling"
    max_admin_user_ids: str = ""
    max_admin_chat_ids: str = ""
    max_webhook_secret: str = "change_me_please"
    max_marketing_broadcast_enabled: bool = False
    public_domain: str = ""
    telegram_proxy: str = ""

    broadcast_delay_seconds: float = Field(default=0.55, ge=0.0)
    media_storage_dir: str = "/app/data/media"

    @property
    def telegram_admin_id_list(self) -> list[int]:
        return _csv_ints(self.telegram_admin_ids)

    @property
    def max_admin_user_id_list(self) -> list[int]:
        return _csv_ints(self.max_admin_user_ids)

    @property
    def max_admin_chat_id_list(self) -> list[int]:
        return _csv_ints(self.max_admin_chat_ids)

    @property
    def max_webhook_url(self) -> str:
        if not self.public_domain:
            return ""
        return f"https://{self.public_domain}/webhooks/max"


@lru_cache
def get_settings() -> Settings:
    return Settings()
