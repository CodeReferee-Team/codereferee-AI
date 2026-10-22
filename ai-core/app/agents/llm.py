import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:
    ChatPromptTemplate = None
    ChatGoogleGenerativeAI = None

from app.config import get_settings


def _strip_fences(text: str) -> str:
    return text.replace("```json", "").replace("```python", "").replace("```", "").strip()


OPENAI_COMPATIBLE = "openai-compatible"


class AgentLLM:
    """Gemini와 OpenAI 호환 로컬 서버(Ollama 등)를 같은 인터페이스로 감싼다."""

    def __init__(self, model: str | None = None, settings=None) -> None:
        # settings를 받는 이유: 평가 러너가 같은 프로세스에서 프로바이더를 갈아끼워 비교한다.
        settings = settings or get_settings()
        self.model = model or settings.llm_model
        self.provider = settings.llm_provider
        self.base_url = settings.llm_base_url
        self.api_key = settings.llm_api_key
        self.timeout = settings.llm_timeout_seconds
        self._llm = None
        if self.provider == OPENAI_COMPATIBLE:
            # 로컬 서버는 키가 없다. 주소가 있으면 쓸 수 있다고 본다.
            self.enabled = bool(self.base_url)
            return
        self.enabled = bool(settings.google_api_key and ChatGoogleGenerativeAI and ChatPromptTemplate)
        if self.enabled:
            self._llm = ChatGoogleGenerativeAI(
                model=self.model,
                google_api_key=settings.google_api_key,
                temperature=0,
                convert_system_message_to_human=True,
            )

    def invoke_text(self, system_prompt: str, user_prompt: str, values: dict[str, Any]) -> str:
        """프롬프트를 직접 치환해 모델에 넘긴다.

        ChatPromptTemplate을 쓰지 않는 이유: 프롬프트에 담긴 JSON 예시의 중괄호를
        템플릿 변수로 해석해 KeyError를 낸다. 값 치환은 우리가 하면 되는 일이라
        템플릿 엔진을 끼울 이유가 없다.
        """
        if not self.enabled:
            raise RuntimeError("LLM is not configured")
        filled = user_prompt
        for key, value in values.items():
            filled = filled.replace("{" + key + "}", str(value))
        if self.provider == OPENAI_COMPATIBLE:
            return _strip_fences(self._invoke_openai_compatible(system_prompt, filled))
        response = self._llm.invoke([("system", system_prompt), ("user", filled)])
        return _strip_fences(str(response.content))

    def _invoke_openai_compatible(self, system_prompt: str, user_prompt: str) -> str:
        """/v1/chat/completions를 직접 호출한다.

        langchain-openai를 새로 넣지 않는 이유: 이 엔드포인트는 stdlib으로 부르면 끝이고,
        의존성이 하나 늘면 로컬 모델로 갈아끼울 때 또 하나를 맞춰야 한다.
        """
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            # Agent 출력은 전부 strict JSON이다. 서버가 강제해주면 스키마 수리 재시도가 줄어든다.
            "response_format": {"type": "json_object"},
            "stream": False,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # 호스티드 API(OpenRouter/Groq/DashScope 등)는 bearer 토큰이 필요하다.
        # 키 없는 로컬 서버(Ollama/vLLM)면 헤더를 넣지 않아 그대로 동작한다.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{str(self.base_url).rstrip('/')}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else exc.reason
            raise RuntimeError(f"Local LLM HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Local LLM unreachable at {self.base_url}: {exc}") from exc
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"Local LLM returned no choices: {body}")
        return str(choices[0].get("message", {}).get("content", ""))

    def invoke_schema_repair(
        self,
        *,
        schema_name: str,
        schema_json: dict[str, Any],
        original_response: str,
        validation_error: str,
    ) -> str:
        return self.invoke_text(
            "You repair invalid Agent JSON. Return only strict JSON. Do not change the intended decision, "
            "do not add unsupported evidence, and do not include markdown fences. "
            "Never copy the validation error text or the schema's own wording into a field value: "
            "fill missing fields from the original response's content.",
            "Schema name: {schema_name}\nSchema JSON: {schema_json}\nValidation error: {validation_error}\n"
            "Original response: {original_response}",
            {
                "schema_name": schema_name,
                "schema_json": json.dumps(schema_json, ensure_ascii=False, sort_keys=True),
                "validation_error": validation_error,
                "original_response": original_response,
            },
        )


def parse_json(text: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = parse_json_strict(text)
        return value if isinstance(value, dict) else fallback
    except (json.JSONDecodeError, ValueError):
        return fallback


def parse_json_strict(text: str) -> dict[str, Any]:
    value = json.loads(_strip_fences(text))
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


llm = AgentLLM()
