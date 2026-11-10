from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./support.db"
    tg_bot_token: str = ""
    tg_group_id: int = 0
    tg_webhook_secret: str = ""
    max_bot_token: str = ""
    max_webhook_secret: str = ""
    public_base_url: str = ""
    admin_ids: str = ""
    project_name: str = "Support Desk"
    ui_language: str = "ru"
    storage_dir: str = "./data/materials"
    max_api_base: str = "https://platform-api2.max.ru"
    welcome_text: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def admins(self) -> set[int]:
        return {int(item.strip()) for item in self.admin_ids.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
