import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.agents import llm as llm_module
from app.config import get_settings


def _local_settings(**overrides):
    base = {
        "llm_provider": llm_module.OPENAI_COMPATIBLE,
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "llama3.1:8b",
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def _response(content: str):
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
    stub = mock.MagicMock()
    stub.__enter__.return_value.read.return_value = body
    return stub


class LocalProviderTests(unittest.TestCase):
    """로컬 모델은 /v1/chat/completions만 있으면 쓸 수 있어야 한다. API 키는 없다."""

    def _build(self, **overrides) -> llm_module.AgentLLM:
        with mock.patch.object(llm_module, "get_settings", return_value=_local_settings(**overrides)):
            return llm_module.AgentLLM()

    def test_enabled_without_an_api_key(self) -> None:
        agent = self._build(google_api_key=None)
        self.assertTrue(agent.enabled)
        self.assertEqual(agent.model, "llama3.1:8b")

    def test_disabled_without_a_base_url(self) -> None:
        self.assertFalse(self._build(llm_base_url=None).enabled)

    def test_request_targets_chat_completions_and_asks_for_json(self) -> None:
        agent = self._build()
        with mock.patch.object(llm_module, "urlopen", return_value=_response('{"ok": true}')) as opener:
            text = agent.invoke_text("sys", "repo={repository_url}", {"repository_url": "u"})
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://localhost:11434/v1/chat/completions")
        self.assertEqual(payload["model"], "llama3.1:8b")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["temperature"], 0)
        # 값 치환은 우리가 한다. 프롬프트에 자리표시자가 남으면 모델이 그대로 읽는다.
        self.assertEqual(payload["messages"][1]["content"], "repo=u")
        self.assertEqual(text, '{"ok": true}')

    def test_markdown_fences_are_stripped(self) -> None:
        agent = self._build()
        fenced = '```json\n{"status": "Pass"}\n```'
        with mock.patch.object(llm_module, "urlopen", return_value=_response(fenced)):
            self.assertEqual(agent.invoke_text("s", "u", {}), '{"status": "Pass"}')

    def test_unreachable_server_raises_with_the_address(self) -> None:
        agent = self._build()
        with mock.patch.object(llm_module, "urlopen", side_effect=URLError("refused")):
            with self.assertRaises(RuntimeError) as caught:
                agent.invoke_text("s", "u", {})
        self.assertIn("http://localhost:11434/v1", str(caught.exception))

    def test_http_error_body_is_surfaced(self) -> None:
        agent = self._build()
        error = HTTPError("url", 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}'))
        with mock.patch.object(llm_module, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                agent.invoke_text("s", "u", {})
        # 모델 이름이 틀렸는지 서버가 죽었는지 구분되어야 한다.
        self.assertIn("model not found", str(caught.exception))

    def test_empty_choices_is_an_error_not_an_empty_report(self) -> None:
        agent = self._build()
        stub = mock.MagicMock()
        stub.__enter__.return_value.read.return_value = b'{"choices": []}'
        with mock.patch.object(llm_module, "urlopen", return_value=stub):
            with self.assertRaises(RuntimeError):
                agent.invoke_text("s", "u", {})

    def test_gemini_stays_the_default_provider(self) -> None:
        self.assertEqual(get_settings().llm_provider, "gemini")


if __name__ == "__main__":
    unittest.main()
