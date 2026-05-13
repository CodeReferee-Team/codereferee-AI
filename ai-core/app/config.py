from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_api_key: str | None = None
    sandbox_image: str = "python:3.12-slim"
    sandbox_timeout_seconds: int = 20
    sandbox_memory_limit: str = "128m"
    sandbox_nano_cpus: int = 500_000_000
    max_self_healing_retries: int = 3

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
