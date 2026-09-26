from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_api_key: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    redis_workflow_queue: str = "codereferee:workflow:input"
    redis_output_queue: str = "codereferee:workflow:output"
    sandbox_image: str = "codereferee/sandbox-multi:1"
    sandbox_base_url: str | None = None
    sandbox_repository_path: str = "/repositories/validate"
    sandbox_http_timeout_seconds: int = 60
    sandbox_timeout_seconds: int = 600
    sandbox_memory_limit: str = "2g"
    sandbox_nano_cpus: int = 2_000_000_000
    sandbox_pids_limit: int = 512
    repository_clone_timeout_seconds: int = 30
    max_self_healing_retries: int = 3
    sqlite_patch_db_path: str = ".codereferee/codereferee.sqlite3"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
