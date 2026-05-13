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


def parse_json(text: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(_strip_fences(text))
        return value if isinstance(value, dict) else fallback
    except json.JSONDecodeError:
        return fallback


llm = AgentLLM()
