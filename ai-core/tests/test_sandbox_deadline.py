"""샌드박스 요청에 AI가 기다릴 시간(deadlineSeconds)을 싣는다.

AI는 응답을 sandbox_http_timeout_seconds(기본 600초)까지 기다리는데, 샌드박스는 그걸 모르고
배포에만 1,200초를 쓸 수 있었다. AI가 먼저 포기하면 판정 불가가 나고 샌드박스는 계속 돈다.
응답이 돌아올 몫을 빼고 남은 시간을 기한으로 보낸다. 기한을 모르는 옛 샌드박스는 이 필드를 버린다.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.sandbox import docker_runner
from app.sandbox.docker_runner import SandboxRunner


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps({"exitCode": 0, "observationStatus": "observed"}).encode()


def _payload(http_timeout: int, **options) -> dict:
    runner = SandboxRunner()
    runner.settings = SimpleNamespace(sandbox_base_url="http://sandbox:8100", sandbox_repository_path="/repositories/validate",
                                      sandbox_http_timeout_seconds=http_timeout, local_sandbox_enabled=False)
    with patch.object(docker_runner, "urlopen", return_value=_Response()) as opener:
        runner.run_repository("https://github.com/o/r", chaos_mode="suite_standard", **options)
    request = opener.call_args.args[0]
    return json.loads(request.data)


class SandboxDeadlineTests(unittest.TestCase):
    def test_deadline_leaves_room_for_the_response(self) -> None:
        payload = _payload(600)
        self.assertEqual(payload["deadlineSeconds"], 600 - docker_runner.SANDBOX_RESPONSE_MARGIN_SECONDS)

    def test_deadline_never_goes_below_the_sandbox_minimum(self) -> None:
        self.assertEqual(_payload(70)["deadlineSeconds"], 60)


class TargetUsersContractTests(unittest.TestCase):
    """요청자의 예상 동시 사용자 수가 BE 큐 → AI → 샌드박스로 끊기지 않고 간다."""

    def test_target_users_reach_the_sandbox_only_when_given(self) -> None:
        self.assertNotIn("targetUsers", _payload(600))
        self.assertEqual(_payload(600, target_users=150)["targetUsers"], 150)

    def test_server_queue_payload_carries_target_users_into_the_state(self) -> None:
        from app.workflow import repository_validation as workflow

        state = workflow._state_from_queue_payload({"taskId": "t-1", "repositoryUrl": "https://github.com/o/r",
                                                    "chaosMode": "suite_standard", "targetUsers": 150})
        self.assertEqual(state.target_users, 150)
        self.assertEqual(workflow._sandbox_contract_options(state)["target_users"], 150)


if __name__ == "__main__":
    unittest.main()
