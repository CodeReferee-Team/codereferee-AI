import unittest

from app.agents import nodes
from app.models import SandboxResult

# 실제 sandbox 스크립트가 항상 출력하는 서두. 이것 때문에 로그 전체 substring 매칭이 깨졌다.
PREAMBLE = """[CodeReferee] installing sandbox clone tools
Get:1 http://deb.debian.org/debian bookworm InRelease
apt-get install -y --no-install-recommends git ca-certificates
[CodeReferee] cloning repository
Cloning into '/tmp/repository'...
[CodeReferee] resolving commit
a1b2c3d4
[CodeReferee] detecting project stack
detected_stack=python
"""


def _result(log: str, exit_code: int = 1) -> SandboxResult:
    return SandboxResult(exit_code=exit_code, stderr=log)


class NonzeroExitCategoryTests(unittest.TestCase):
    """실패 단계는 마지막 스테이지 마커로 정한다. 로그 전체를 뒤지면 서두에 걸린다."""

    def test_compile_failure_is_not_called_a_dependency_failure(self) -> None:
        # 회귀 방지: 코퍼스 파일럿이 실제 로그에서 찾은 버그다.
        # apt-get "install"이 서두에 있어 모든 실패가 dependency_install_failed로 분류됐다.
        log = PREAMBLE + "*** Error compiling './six.py'...\nSyntaxError: invalid syntax\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "sandbox_nonzero_exit")

    def test_pip_resolution_failure_is_a_dependency_failure(self) -> None:
        log = PREAMBLE + (
            "ERROR: Could not find a version that satisfies the requirement nope-zzz\n"
            "ERROR: No matching distribution found for nope-zzz\n"
        )
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "dependency_install_failed")

    def test_failing_tests_are_a_test_failure(self) -> None:
        log = PREAMBLE + (
            "Collecting pytest\nSuccessfully installed pytest-8.0.0\n"
            "=========================== FAILURES ===========================\n"
            "E       AssertionError: assert 1 == 2\n"
            "=========== short test summary info ============\n1 failed\n"
        )
        # pytest를 설치하는 과정에도 install이 찍힌다. 그래도 테스트 실패로 나와야 한다.
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "test_failure")

    # 아래 두 건은 로그 문구가 아니라 sandbox가 보낸 failed_step으로 판별한다. 원본은
    # 스테이지 마커를 로그에서 찾았는데, 구조화된 결과가 생긴 뒤로는 그 경로를 쓰지 않는다.
    # 판별 의도는 그대로 두고 입력만 바꿨다.
    def test_clone_failure_is_reported_as_an_inaccessible_repository(self) -> None:
        result = _result("fatal: repository not found\n")
        result.sandbox_report = {"failed_step": "clone"}
        self.assertEqual(nodes._nonzero_exit_category(result), "repository_not_accessible")

    def test_failure_before_the_clone_means_the_project_never_ran(self) -> None:
        result = _result("E: Unable to locate package git\n")
        result.sandbox_report = {"failed_step": "prepare"}
        self.assertEqual(nodes._nonzero_exit_category(result), "sandbox_not_executed")

    def test_exit_code_86_means_no_manifest(self) -> None:
        log = PREAMBLE + "No supported project manifest found\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log, 86)), "no_manifest_detected")

    def test_exit_code_87_means_unsupported_stack(self) -> None:
        log = PREAMBLE + "No Gradle wrapper in sandbox image\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log, 87)), "unsupported_project_stack")

    def test_docker_failure_without_any_stage_marker(self) -> None:
        self.assertEqual(
            nodes._nonzero_exit_category(_result("Docker repository sandbox error: dockerfile missing")),
            "docker_build_failed",
        )

    def test_unrecognised_failure_falls_back_without_guessing(self) -> None:
        log = PREAMBLE + "Segmentation fault\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "sandbox_nonzero_exit")


class ExternalSandboxLogTests(unittest.TestCase):
    """외부 sandbox HTTP 응답에는 우리 단계 마커가 없다. 그때는 전체를 봐도 된다."""

    def test_pytest_failure_without_stage_markers(self) -> None:
        self.assertEqual(nodes._nonzero_exit_category(_result("pytest failed")), "test_failure")

    def test_dependency_failure_without_stage_markers(self) -> None:
        log = "npm ERR! code ERESOLVE\nnpm ERR! could not resolve dependencies"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "dependency_install_failed")


class SignPriorityTests(unittest.TestCase):
    """컴파일 실패가 테스트 실패보다 구체적이다. 둘이 같이 나오면 컴파일 쪽이다."""

    def test_short_make_failure_is_a_test_failure(self) -> None:
        log = PREAMBLE + "make: *** [test] Error 1\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "test_failure")

    def test_bare_failure_count_is_not_called_a_test_failure(self) -> None:
        # "1 failed"만으로는 무엇이 실패했는지 알 수 없다. 단정하지 않고 일반 실패로 둔다.
        self.assertEqual(nodes._nonzero_exit_category(_result(PREAMBLE + "1 failed")), "sandbox_nonzero_exit")

    def test_compile_failure_wins_over_a_stray_failed_word(self) -> None:
        log = PREAMBLE + "*** Error compiling './six.py'...\nSyntaxError: invalid syntax\n1 failed\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "sandbox_nonzero_exit")

    def test_dependency_failure_wins_over_the_word_failed(self) -> None:
        log = PREAMBLE + "ERROR: No matching distribution found for nope-zzz\ninstallation failed\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "dependency_install_failed")


class NonTestStepFailureTests(unittest.TestCase):
    """테스트가 아닌 단계의 실패를 테스트 실패로 부르지 않는다. T0-adv 라벨 기준."""

    def test_publish_step_failure_is_not_a_test_failure(self) -> None:
        result = SandboxResult(
            exit_code=1, stderr="gradle publish failed", stdout="BUILD SUCCESSFUL in 12s\npublish step failed\n"
        )
        self.assertEqual(nodes._nonzero_exit_category(result), "sandbox_nonzero_exit")

    def test_long_log_without_any_test_signal(self) -> None:
        log = "\n".join(f"[info] warming cache shard {i}" for i in range(60)) + "\n[error] shard 60 timed out\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "sandbox_nonzero_exit")

    def test_test_word_plus_failure_word_is_a_test_failure(self) -> None:
        result = SandboxResult(exit_code=1, stderr="1 failed", stdout="running tests...\n")
        self.assertEqual(nodes._nonzero_exit_category(result), "test_failure")


# 로그에서 실패 지점 이후만 잘라 근거로 남기던 _failure_line·_log_evidence 테스트는
# 가져오지 않았다. 그 구현은 채택되지 않았고, 지금은 sandbox가 보낸 steps[]와
# failed_step이 같은 일을 구조화된 형태로 한다. 로그를 잘라 읽는 쪽으로 되돌릴 이유가 없다.


if __name__ == "__main__":
    unittest.main()
