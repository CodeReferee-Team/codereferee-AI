"""레포가 검증 방법을 선언하면 그대로 돌리고, 아니면 추측했다고 기록한다.

통합 테스트에서 멀쩡한 레포 네 건이 불합격으로 나왔다. itsdangerous는 테스트 전용
의존성이 없어서, debug는 브라우저가 없어서, gson은 멀티모듈 순서 때문에, okio는 Android
SDK가 없어서다. 전부 우리가 테스트 명령을 추측했기 때문이고 레포 결함이 아니다.

추측이 틀려서 난 실패를 레포 탓으로 돌리면 안 된다. 그래서 두 가지를 둔다.
선언 수단을 주고, 선언 없이 돌린 실패에는 "추측했다"는 표시를 남긴다.
"""

import unittest

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.sandbox import docker_runner


class DeclarationIsReadTests(unittest.TestCase):
    """.codereferee/validation.yaml은 Kubernetes 샌드박스가 이미 쓰는 파일이다.

    1층도 같은 파일을 읽으면 두 층의 입력이 하나가 된다.
    """

    def setUp(self) -> None:
        self.script = docker_runner._repository_validation_script(
            "https://github.com/o/r", None, None, patch_file=""
        )

    def test_the_declaration_file_is_looked_up(self) -> None:
        self.assertIn(".codereferee/validation.yaml", self.script)

    def test_a_declared_test_command_replaces_the_guess(self) -> None:
        self.assertIn("DECLARED_TEST", self.script)

    def test_declared_test_dependencies_are_installed(self) -> None:
        self.assertIn("DECLARED_TEST_DEPS", self.script)

    def test_the_report_says_whether_we_guessed(self) -> None:
        # 추측인지 선언인지는 판정이 써야 하는 신호다. 구조화 결과에 담는다.
        self.assertIn('"verification_declared"', self.script)


class GuessedRunIsMarkedTests(unittest.TestCase):
    COLLECTION_ERROR = (
        "ImportError while importing test module 'tests/test_timed.py'.\n"
        "E   ModuleNotFoundError: No module named 'freezegun'\n2 errors in 0.08s"
    )
    TEST_FAILURE = "FAILED tests/test_cache.py::test_roundtrip - assert 1 == 2\n1 failed, 3 passed"

    def _state(self, exit_code: int, report: dict, stderr: str = "") -> AgentState:
        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.execution_result = SandboxResult(
            exit_code=exit_code, stderr=stderr, sandbox_report=report
        )
        return state

    def test_a_collection_error_is_not_the_repository_s_fault(self) -> None:
        """pytest는 수집이 깨지면 2로, 테스트가 실패하면 1로 끝낸다.

        수집 오류는 대개 테스트 전용 의존성이 없는 것이고, 그것을 우리가 설치하지 못한
        것이다. 레포 결함으로 보고하면 사용자가 멀쩡한 코드를 불합격으로 받는다.
        """
        state = self._state(2, {"detected_stack": "python", "failed_step": "smoke",
                                "verification_declared": False}, self.COLLECTION_ERROR)
        report = nodes._fallback_judge(state)
        self.assertEqual(report["reason_category"], "verification_environment_unsupported")

    def test_a_declared_run_that_fails_collection_is_the_repository_s_fault(self) -> None:
        # 선언한 명령으로 돌렸는데 깨졌으면 추측 탓이 아니다.
        state = self._state(2, {"detected_stack": "python", "failed_step": "smoke",
                                "verification_declared": True}, self.COLLECTION_ERROR)
        report = nodes._fallback_judge(state)
        self.assertEqual(report["reason_category"], "test_failure")

    def test_a_plain_test_failure_stays_a_test_failure(self) -> None:
        # exit 1은 테스트가 돌았고 실패한 것이다. 선언 여부와 무관하다.
        for declared in (True, False):
            state = self._state(1, {"detected_stack": "python", "failed_step": "smoke",
                                    "verification_declared": declared}, self.TEST_FAILURE)
            self.assertEqual(nodes._fallback_judge(state)["reason_category"], "test_failure", declared)


class EnvironmentLimitsAreNotVerdictsTests(unittest.TestCase):
    """우리가 검증할 수 없었던 것은 레포의 불합격이 아니다.

    아키텍처가 ERROR를 "인프라 오류로 판정 불가"로, FAILED를 "코드 결함"으로 정의한다.
    환경 한계는 정의상 ERROR다.
    """

    def _state(self, exit_code: int) -> AgentState:
        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.execution_result = SandboxResult(
            exit_code=exit_code, sandbox_report={"detected_stack": "python", "failed_step": "smoke"}
        )
        return state

    def test_a_generic_exit_code_is_not_claimed_as_ours(self) -> None:
        """종료 코드는 도구마다 뜻이 다르다. npm의 127은 "명령 없음"이 아니고
        make의 2는 pytest 수집 오류가 아니다. 코드만 보고 환경 탓으로 돌리면 안 된다."""
        node = self._state(127)
        node.execution_result.sandbox_report = {"detected_stack": "node", "failed_step": "smoke"}
        self.assertNotEqual(
            nodes._fallback_judge(node)["reason_category"],
            "verification_environment_unsupported",
        )

    def test_our_memory_ceiling_is_ours(self) -> None:
        # 137은 우리가 건 sandbox_memory_limit에 걸린 것이다.
        self.assertEqual(
            nodes._fallback_judge(self._state(137))["reason_category"],
            "verification_environment_unsupported",
        )

    def test_a_pytest_usage_error_is_ours(self) -> None:
        # 4는 pytest 사용법 오류다. 우리가 부르는 방법이 틀렸다는 뜻이다.
        self.assertEqual(
            nodes._fallback_judge(self._state(4))["reason_category"],
            "verification_environment_unsupported",
        )

    def test_no_runner_keeps_its_own_code(self) -> None:
        # 87은 unsupported_project_stack이라는 정확한 코드를 이미 갖고 있다. 묶으면
        # 어떤 스택을 못 받았는지가 사라진다. 환경 한계인 것은 사유 코드 분류로 표현한다.
        self.assertEqual(
            nodes._fallback_judge(self._state(87))["reason_category"],
            "unsupported_project_stack",
        )


if __name__ == "__main__":
    unittest.main()
