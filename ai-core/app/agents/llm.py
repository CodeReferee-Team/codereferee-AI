import json
from typing import Any

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_google_genai import ChatGoogleGenerativeAI
except ImportError:
    ChatPromptTemplate = None
    ChatGoogleGenerativeAI = None

from app.config import get_settings


def _strip_fences(text: str) -> str:
    return text.replace("```json", "").replace("```python", "").replace("```", "").strip()


class AgentLLM:
    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = bool(settings.google_api_key and ChatGoogleGenerativeAI and ChatPromptTemplate)
        self._llm = None
        if self.enabled:
            self._llm = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=settings.google_api_key,
                temperature=0,
                convert_system_message_to_human=True,
            )

    def invoke_text(self, system_prompt: str, user_prompt: str, values: dict[str, Any]) -> str:
        if not self._llm:
            raise RuntimeError("LLM is not configured")
        prompt = ChatPromptTemplate.from_messages(
            [("system", system_prompt), ("user", user_prompt)]
        )
        response = (prompt | self._llm).invoke(values)
        return _strip_fences(str(response.content))

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
            "do not add unsupported evidence, and do not include markdown fences.",
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
