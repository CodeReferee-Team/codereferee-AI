import unittest

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.sandbox import docker_runner


class ScriptContractTests(unittest.TestCase):
    """테스트가 없는 레포를 합격으로 내보내면 안 된다. 컴파일과 검증은 다르다."""

    def setUp(self) -> None:
        self.script = docker_runner._repository_validation_script(
            "https://github.com/o/r", None, None, patch_file=""
        )

    def test_python_without_a_tests_directory_does_not_pass(self) -> None:
        self.assertIn(
            f'[ -d tests ] || {{ echo "No tests directory to verify"; return {docker_runner.NOTHING_TO_VERIFY_EXIT_CODE}; }}',
            self.script,
        )

    def test_node_checks_for_a_test_script_before_running_it(self) -> None:
        # npm run test --if-present는 test 스크립트가 없으면 성공으로 끝낸다.
        self.assertIn("process.exit(s.test?0:1)", self.script)
        self.assertIn(f"return {docker_runner.NOTHING_TO_VERIFY_EXIT_CODE}", self.script)

    def test_the_if_present_shortcut_is_gone(self) -> None:
        self.assertNotIn("npm run test --if-present", self.script)


class FailureReasonTests(unittest.TestCase):
    """종료 코드 숫자만 문장에 넣으면 사용자가 원인을 알 수 없다."""

    def _result(self, exit_code: int) -> SandboxResult:
        return SandboxResult(exit_code=exit_code, sandbox_report={"detected_stack": "python", "failed_step": "smoke"})

    def test_nothing_to_verify_says_why(self) -> None:
        reason = nodes._sandbox_failure_reason(self._result(docker_runner.NOTHING_TO_VERIFY_EXIT_CODE))
        self.assertIn("no tests", reason)
        self.assertIn("Compiling is not verification", reason)

    def test_no_manifest_says_why(self) -> None:
        self.assertIn("manifest", nodes._sandbox_failure_reason(self._result(docker_runner.NO_MANIFEST_EXIT_CODE)))

    def test_ordinary_failure_still_names_the_failed_step(self) -> None:
        reason = nodes._sandbox_failure_reason(self._result(1))
        self.assertIn("smoke", reason)
        self.assertIn("python", reason)

    def test_a_test_less_repository_is_judged_failed(self) -> None:
        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.execution_result = self._result(docker_runner.NOTHING_TO_VERIFY_EXIT_CODE)
        report = nodes._fallback_judge(state)
        self.assertEqual(report["status"], "Fail")
        self.assertIn("no tests", report["reason"])


if __name__ == "__main__":
    unittest.main()


class PythonDependencyStepTests(unittest.TestCase):
    """pyproject.toml만 있는 레포도 설치해야 한다.

    requirements.txt만 보면 현대 Python 레포에는 아무것도 설치되지 않는다. 그러면 pytest가
    패키지를 import하지 못해 수집 단계에서 깨지고, 멀쩡한 레포가 test_failure로 판정된다.
    실측: pallets/itsdangerous가 exit 2 / "5 errors"로 불합격 처리됐다.
    """

    def setUp(self) -> None:
        self.script = docker_runner._repository_validation_script(
            "https://github.com/o/r", None, None, patch_file=""
        )

    def test_a_pyproject_only_repository_installs_itself(self) -> None:
        self.assertIn("[ -f pyproject.toml ] || [ -f setup.py ]", self.script)
        self.assertIn("python -m pip install --disable-pip-version-check .", self.script)

    def test_requirements_is_still_installed(self) -> None:
        self.assertIn("-r requirements.txt", self.script)
