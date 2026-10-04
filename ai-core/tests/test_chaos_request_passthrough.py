"""카오스 실행 옵션이 서버에서 샌드박스까지 끊기지 않고 간다.

서버가 Redis payload에 chaosMode와 deploymentProfile을 싣는데(codereferee-server #8)
AI가 그 두 키를 읽지 않아 카오스 경로가 켜지지 않았다. 오류도 나지 않고 일반 검증으로
돌아서, 사용자는 안정성 검사를 요청했는데 빌드 확인 결과를 받았다.
"""

import json
import unittest
from unittest import mock

from app.models import RepositoryValidationRequest
from app.sandbox import docker_runner
from app.workflow import repository_validation as workflow


class RequestFieldTests(unittest.TestCase):
    def test_the_request_carries_the_chaos_options(self) -> None:
        request = RepositoryValidationRequest(
            repository_url="https://github.com/o/r",
            chaos_mode="litmus_pod_delete",
            deployment_profile="quickbyte-demo",
        )
        self.assertEqual(request.chaos_mode, "litmus_pod_delete")
        self.assertEqual(request.deployment_profile, "quickbyte-demo")

    def test_an_unknown_mode_is_rejected_here_not_at_the_sandbox(self) -> None:
        # 오타 하나가 서버와 Redis와 AI를 지나 마지막 샌드박스 호출에서 422로 드러나면
        # 사용자가 받는 오류가 쓸모없다. 받는 자리에서 거른다.
        with self.assertRaises(ValueError):
            RepositoryValidationRequest(
                repository_url="https://github.com/o/r", chaos_mode="litmus_pod_dlete"
            )

    def test_a_profile_needs_a_mode_that_deploys(self) -> None:
        # 샌드박스는 litmus_container_kill에 프로필을 주면 422를 돌려준다(app/main.py 191행).
        with self.assertRaises(ValueError):
            RepositoryValidationRequest(
                repository_url="https://github.com/o/r",
                chaos_mode="litmus_container_kill",
                deployment_profile="quickbyte-demo",
            )

    def test_a_profile_name_stays_a_plain_name(self) -> None:
        # 이 값은 샌드박스에서 profiles/{name}.json 경로 조회로 들어간다.
        with self.assertRaises(ValueError):
            RepositoryValidationRequest(
                repository_url="https://github.com/o/r",
                chaos_mode="litmus_pod_delete",
                deployment_profile="../../etc/passwd",
            )

    def test_no_chaos_options_is_still_valid(self) -> None:
        request = RepositoryValidationRequest(repository_url="https://github.com/o/r")
        self.assertIsNone(request.chaos_mode)
        self.assertIsNone(request.deployment_profile)


class QueueRoundTripTests(unittest.TestCase):
    """서버 스키마(camelCase)와 AI 스키마(snake_case) 양쪽에서 살아남아야 한다."""

    def test_the_server_payload_keys_are_read(self) -> None:
        state = workflow._state_from_queue_payload(
            {
                "taskId": "t1",
                "repositoryUrl": "https://github.com/o/r",
                "branch": "main",
                "commitSha": "abc",
                "chaosMode": "deployment_scale_down",
                "deploymentProfile": "quickbyte-demo",
            }
        )
        self.assertEqual(state.chaos_mode, "deployment_scale_down")
        self.assertEqual(state.deployment_profile, "quickbyte-demo")

    def test_our_own_payload_round_trips(self) -> None:
        request = RepositoryValidationRequest(
            repository_url="https://github.com/o/r",
            chaos_mode="rollout_restart",
            deployment_profile="quickbyte-demo-ha",
        )
        state = workflow.create_validation_state(request, job_id="t2")
        restored = workflow._state_from_queue_payload(workflow._queue_payload(state))
        self.assertEqual(restored.chaos_mode, "rollout_restart")
        self.assertEqual(restored.deployment_profile, "quickbyte-demo-ha")


class SandboxRequestTests(unittest.TestCase):
    def _captured_body(self, **kwargs) -> dict:
        captured: dict = {}

        class _Response:
            def read(self):
                return b'{"exit_code": 0}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _urlopen(request, timeout=None):
            captured.update(json.loads(request.data.decode("utf-8")))
            return _Response()

        runner = docker_runner.SandboxRunner()
        runner.settings = runner.settings.model_copy(update={"sandbox_base_url": "http://sandbox"})
        with mock.patch.object(docker_runner, "urlopen", _urlopen):
            runner.run_repository("https://github.com/o/r", **kwargs)
        return captured

    def test_the_options_reach_the_sandbox_body(self) -> None:
        body = self._captured_body(chaos_mode="litmus_pod_delete", deployment_profile="quickbyte-demo")
        self.assertEqual(body["chaosMode"], "litmus_pod_delete")
        self.assertEqual(body["deploymentProfile"], "quickbyte-demo")

    def test_a_plain_run_sends_no_chaos_keys(self) -> None:
        body = self._captured_body()
        self.assertNotIn("chaosMode", body)
        self.assertNotIn("deploymentProfile", body)


class LocalDockerCannotDoChaosTests(unittest.TestCase):
    """로컬 Docker 경로는 쿠버네티스가 없어 장애 주입을 할 수 없다.

    그런데도 그냥 빌드 검증을 돌려 통과로 내보내면, 안정성 검사를 요청한 사용자가
    실험이 돌지 않았다는 사실을 모른 채 합격을 받는다.
    """

    def test_a_chaos_request_without_a_remote_sandbox_is_an_infra_error(self) -> None:
        runner = docker_runner.SandboxRunner()
        runner.settings = runner.settings.model_copy(update={"sandbox_base_url": None})
        result = runner.run_repository("https://github.com/o/r", chaos_mode="litmus_pod_delete")
        self.assertEqual(result.infra_error, "chaos_requires_remote_sandbox")
        self.assertIsNone(result.exit_code)


if __name__ == "__main__":
    unittest.main()
