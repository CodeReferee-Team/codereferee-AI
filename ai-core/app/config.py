from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_api_key: str | None = None
    # gemini | openai-compatible. openai-compatible은 Ollama, llama.cpp server, vLLM처럼
    # /v1/chat/completions를 노출하는 로컬 서버를 가리킨다. 최종 목표가 로컬 모델이라
    # 클라우드 API에 묶이지 않게 갈아끼울 자리를 둔다.
    # 모델명을 코드에 박지 않는다. 신규 키에서 구모델이 막히는 일이 있어 교체가 잦다.
    llm_model: str = "gemini-flash-latest"
    llm_provider: str = "gemini"
    # openai-compatible일 때의 서버 주소. 예: http://localhost:11434/v1 (Ollama)
    llm_base_url: str | None = None
    # 로컬 모델은 첫 토큰까지 오래 걸린다. 클라우드 기준으로 잡으면 멀쩡한 호출이 끊긴다.
    llm_timeout_seconds: int = 120
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
    # 판정은 규칙이 한다. 측정 결과 LLM은 판정 정확도가 같거나 낮고(100% vs 94%) 원인 분류는
    # 훨씬 낮았으며(94.1% vs 58.8%), 레포 로그에 심어둔 지시에 흔들렸다(0건 vs 2건).
    # docs/evaluation-design.md 12절. 비교 실험을 다시 돌릴 수 있도록 경로는 남겨 둔다.
    judge_uses_llm: bool = False
    planner_uses_llm: bool = False
    max_self_healing_retries: int = 3
    # 1MB가 넘는 diff는 수정 범위가 과도하다는 뜻이라 신뢰하기 어렵다.
    max_patch_diff_bytes: int = 1_000_000
    sqlite_patch_db_path: str = ".codereferee/codereferee.sqlite3"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
