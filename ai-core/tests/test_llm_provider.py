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


class TransportFailureDegradesTests(unittest.TestCase):
    """LLM에 닿지 못해도 작업 전체를 버리면 안 된다.

    판정은 규칙이 이미 냈다. Critic의 서술과 Refiner의 수정안은 거기에 얹는 설명이다.
    로컬 모델이 느려 타임아웃 하나 났다고 판정까지 잃으면, 검증 결과를 LLM 가용성에
    묶는 셈이 된다.
    """

    def _failed_state(self):
        from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.execution_result = SandboxResult(exit_code=1, stderr="AssertionError")
        return state

    def test_critic_falls_back_when_the_model_is_unreachable(self) -> None:
        from app.agents import nodes

        state = self._failed_state()
        with mock.patch.object(nodes.llm, "enabled", True), mock.patch.object(
            nodes.llm, "invoke_text", side_effect=RuntimeError("Local LLM unreachable: timed out")
        ):
            state = nodes.critic_node(state)
        self.assertTrue(state.critic_feedback.get("root_cause"))
        self.assertTrue(any("unreachable" in event.lower() for event in state.events))

    def test_refiner_falls_back_when_the_model_is_unreachable(self) -> None:
        from app.agents import nodes

        state = self._failed_state()
        with mock.patch.object(nodes.llm, "enabled", True), mock.patch.object(
            nodes.llm, "invoke_text", side_effect=RuntimeError("Local LLM unreachable: timed out")
        ):
            state = nodes.refiner_node(state)
        self.assertTrue(state.refiner_report.get("summary"))


class AuthHeaderTests(unittest.TestCase):
    """호스티드 openai-compatible API는 bearer 토큰이 필요하고, 로컬 서버는 없어야 한다."""

    def _build(self, **overrides) -> llm_module.AgentLLM:
        with mock.patch.object(llm_module, "get_settings", return_value=_local_settings(**overrides)):
            return llm_module.AgentLLM()

    def _request(self, agent) -> object:
        with mock.patch.object(llm_module, "urlopen", return_value=_response('{"ok": true}')) as opener:
            agent.invoke_text("sys", "u", {})
        return opener.call_args.args[0]

    def test_bearer_header_added_when_api_key_set(self) -> None:
        agent = self._build(llm_api_key="sk-test-123")
        self.assertEqual(self._request(agent).get_header("Authorization"), "Bearer sk-test-123")

    def test_no_auth_header_for_keyless_local_server(self) -> None:
        agent = self._build(llm_api_key=None)
        self.assertIsNone(self._request(agent).get_header("Authorization"))
