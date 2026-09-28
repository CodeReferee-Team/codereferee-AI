from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_api_key: str | None = None
    # gemini | openai-compatible. openai-compatible은 Ollama, llama.cpp server, vLLM처럼
    # /v1/chat/completions를 노출하는 로컬 서버를 가리킨다. 최종 목표가 로컬 모델이라
    # 클라우드 API에 묶이지 않게 갈아끼울 자리를 둔다.
    llm_provider: str = "gemini"
    # openai-compatible일 때의 서버 주소. 예: http://localhost:11434/v1 (Ollama)
    llm_base_url: str | None = None
    # 모델명을 코드에 박지 않는다. 신규 키에서 구모델이 막히는 일이 있어 교체가 잦다.
    llm_model: str = "gemini-flash-latest"
    # 로컬 모델은 첫 토큰까지 오래 걸린다. 클라우드 기준으로 잡으면 멀쩡한 호출이 끊긴다.
    llm_timeout_seconds: int = 120
    # 판정은 규칙이 한다. 측정 결과 LLM은 판정 정확도가 같고 원인 분류는 더 낮았으며
    # (docs/evaluation-design.md 12절), 레포 로그에 심어둔 지시에 흔들릴 여지도 남는다.
    # 비교 실험을 다시 돌릴 수 있도록 경로 자체는 남겨 둔다.
    judge_uses_llm: bool = False
    planner_uses_llm: bool = False
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
