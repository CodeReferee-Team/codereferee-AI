import unittest

from app.agents.evidence import classify_failure_category, _primary_signal
from app.models import AgentState, RepositoryPreflightReport, SandboxResult


def _reachable_state(**exec_kwargs) -> AgentState:
    return AgentState(
        job_id="t",
        repository_url="https://github.com/o/r",
        preflight_report=RepositoryPreflightReport(
            repository_url="https://github.com/o/r",
            cloneable=True,
            executable=True,
            reason="reachable",
        ),
        execution_result=SandboxResult(**exec_kwargs),
    )


class FailureClassificationTests(unittest.TestCase):
    """배포 롤아웃 타임아웃은 일반 non_zero_exit과 구분돼야 한다.

    근거: http500(probe 500)·crashloop(부팅 크래시)가 둘 다 'run 단계 kubectl rollout
    타임아웃'으로 들어오는데, 샌박이 pod 진단을 안 실어 근본원인은 구분 불가다. 최소한
    '테스트/빌드가 깨진 non_zero_exit'과 '배포가 준비 상태에 도달 못한 rollout 타임아웃'은
    단계 수준에서 갈라야 Critic이 정직하게 진단한다.
    """

    def test_rollout_timeout_is_its_own_category(self) -> None:
        state = _reachable_state(
            exit_code=1,
            timed_out=False,
            stderr="kubectl --context kind-codereferee: error: timed out waiting for the condition",
            sandbox_report={"failed_step": "run"},
        )
        self.assertEqual(classify_failure_category(state), "deploy_rollout_timeout")
        self.assertIn("rollout", _primary_signal(state, "deploy_rollout_timeout").lower())

    def test_genuine_test_failure_stays_non_zero_exit(self) -> None:
        state = _reachable_state(
            exit_code=1,
            timed_out=False,
            stderr="AssertionError: assert add(2, 3) == 5",
            sandbox_report={"failed_step": "test"},
        )
        self.assertEqual(classify_failure_category(state), "non_zero_exit")

    def test_rollout_marker_without_run_step_not_reclassified(self) -> None:
        # 타임아웃 문자열이 run이 아닌 단계에서 나오면 재분류하지 않는다(정밀 게이트).
        state = _reachable_state(
            exit_code=1,
            timed_out=False,
            stderr="timed out waiting for the condition",
            sandbox_report={"failed_step": "install"},
        )
        self.assertEqual(classify_failure_category(state), "non_zero_exit")


if __name__ == "__main__":
    unittest.main()
