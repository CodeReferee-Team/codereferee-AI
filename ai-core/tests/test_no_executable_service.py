"""세부사항 2호 — 검증 대상을 특정하지 못한 경우는 코드 결함이 아니다.

다양한 레포 테스트에서 프론트(LOCA)·라이브러리/노트북(NotiCE)이 전부 FAILED로 나왔다.
샌드박스가 "실행 가능한 HTTP 서비스를 못 찾음(ConfigurationRequired)"으로 끝낸 것이고,
이건 레포 코드가 깨진 게 아니라 우리가 무엇을 검증할지 못 정한 것이다. 세부사항 1호
(테스트 환경 한계)와 같은 부류 → 판정 불가(ERROR)여야 한다.

구분자는 종료 코드가 아니라 샌드박스의 configuration_required 플래그다. 종료 코드 89는
"테스트 없음(no_tests_detected, 의도된 Fail)"과도 겹치므로 코드로 구분하면 안 된다.
"""

import unittest

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


def _state(exit_code: int, report: dict, stderr: str = "") -> AgentState:
    state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
    state.preflight_report = RepositoryPreflightReport(
        repository_url="https://github.com/o/r", cloneable=True, executable=True
    )
    state.execution_result = SandboxResult(exit_code=exit_code, stderr=stderr, sandbox_report=report)
    return state


class NoExecutableServiceTests(unittest.TestCase):
    MSG = ("No unambiguous executable HTTP service found. Add .codereferee/validation.yaml; "
           "libraries and ambiguous multi-service projects are not auto-deployed.")

    def test_configuration_required_is_a_verification_limit_not_a_code_defect(self) -> None:
        # 프론트/라이브러리처럼 실행 서비스를 못 특정한 경우(어떤 스택이든).
        state = _state(89, {"detected_stack": "javascript", "configuration_required": True}, self.MSG)
        report = nodes._fallback_judge(state)
        self.assertEqual(report["reason_category"], "verification_environment_unsupported")

    def test_flag_wins_regardless_of_exit_code(self) -> None:
        # 플래그가 신호다. 종료 코드가 1이어도 검증 불가로 본다.
        state = _state(1, {"detected_stack": "python", "configuration_required": True}, self.MSG)
        report = nodes._fallback_judge(state)
        self.assertEqual(report["reason_category"], "verification_environment_unsupported")

    def test_no_tests_without_the_flag_stays_a_fail(self) -> None:
        # 반례: 플래그 없는 exit 89는 "테스트 없음"(의도된 Fail)이다. 검증 불가로 뒤집지 않는다.
        state = _state(89, {"detected_stack": "python"})
        report = nodes._fallback_judge(state)
        self.assertNotEqual(report["reason_category"], "verification_environment_unsupported")

    def test_a_real_test_failure_is_still_the_repo_s_fault(self) -> None:
        state = _state(1, {"detected_stack": "python", "failed_step": "smoke",
                           "verification_declared": True},
                       "FAILED tests/test_x.py::test_y - assert 1 == 2\n1 failed")
        report = nodes._fallback_judge(state)
        self.assertNotEqual(report["reason_category"], "verification_environment_unsupported")


if __name__ == "__main__":
    unittest.main()
