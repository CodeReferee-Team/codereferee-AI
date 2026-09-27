from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_api_key: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    redis_workflow_queue: str = "codereferee:workflow:input"
    redis_output_queue: str = "codereferee:workflow:output"
    sandbox_image: str = "python:3.12-slim"
    sandbox_base_url: str | None = None
    sandbox_repository_path: str = "/repositories/validate"
    sandbox_http_timeout_seconds: int = 60
    # 20초로는 실제 레포가 clone과 빌드를 끝내지 못해, 사용자 코드 결함이 아닌 timeout Fail이 났다.
    sandbox_timeout_seconds: int = 180
    sandbox_memory_limit: str = "128m"
    sandbox_nano_cpus: int = 500_000_000
    repository_clone_timeout_seconds: int = 30
    max_self_healing_retries: int = 3
    sqlite_patch_db_path: str = ".codereferee/codereferee.sqlite3"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
