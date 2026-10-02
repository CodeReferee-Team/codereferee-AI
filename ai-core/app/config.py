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
    # 교체 Pod의 스케줄링과 이미지 pull에 드는 시간. 클러스터마다 달라 측정이 불가능하므로
    # 설정으로 둔다. docs/judge-policy.md 6.5의 기대 복구 상한 계산식에 쓴다.
    chaos_recovery_startup_allowance_seconds: float = 30.0
    max_self_healing_retries: int = 3
    # 1MB가 넘는 diff는 수정 범위가 과도하다는 뜻이라 신뢰하기 어렵다.
    max_patch_diff_bytes: int = 1_000_000
    sqlite_patch_db_path: str = ".codereferee/codereferee.sqlite3"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
