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

    def test_clone_failure_is_reported_as_an_inaccessible_repository(self) -> None:
        log = "[CodeReferee] installing sandbox clone tools\napt-get install git\n" \
              "[CodeReferee] cloning repository\nfatal: repository not found\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "repository_not_accessible")

    def test_checkout_failure_is_reported_as_a_missing_ref(self) -> None:
        log = PREAMBLE.split("[CodeReferee] detecting project stack")[0] + \
            "error: pathspec 'deadbeef' did not match any file(s) known to git\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "ref_not_found")

    def test_failure_before_the_clone_means_the_project_never_ran(self) -> None:
        log = "[CodeReferee] installing sandbox clone tools\nE: Unable to locate package git\n"
        self.assertEqual(nodes._nonzero_exit_category(_result(log)), "sandbox_not_executed")

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


if __name__ == "__main__":
    unittest.main()


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


class JudgeEvidenceBoundsTests(unittest.TestCase):
    """판정 근거에 로그 전문을 넣으면 준비 과정이 근거가 된다. Critic이 그걸 베꼈다."""

    def test_reason_is_the_first_meaningful_failure_line(self) -> None:
        log = PREAMBLE + "*** Error compiling './six.py'...\nSyntaxError: invalid syntax\n"
        result = _result(log)
        self.assertEqual(nodes._failure_line(result), "*** Error compiling './six.py'...")

    def test_reason_skips_stage_markers_and_progress_lines(self) -> None:
        log = PREAMBLE + "Listing './docs'...\nCompiling './a.py'...\nBoom: real failure\n"
        self.assertEqual(nodes._failure_line(_result(log)), "Boom: real failure")

    def test_evidence_drops_the_preamble_and_is_bounded(self) -> None:
        log = PREAMBLE + "x" * 5000
        evidence = nodes._log_evidence(_result(log))
        self.assertEqual(len(evidence), 1)
        self.assertNotIn("apt-get install", evidence[0])
        # truncate_log이 잘림 표시를 덧붙이므로 상한에 그 길이를 더해 비교한다.
        self.assertLessEqual(len(evidence[0]), nodes.MAX_EVIDENCE_LOG_CHARS + 40)
        self.assertIn("truncated", evidence[0])

    def test_evidence_is_never_empty(self) -> None:
        # SandboxResult.log는 로그가 비어도 실행 요약을 담는다. 근거가 빈 리스트로 나가지 않는다.
        evidence = nodes._log_evidence(SandboxResult(exit_code=3))
        self.assertEqual(len(evidence), 1)
        self.assertIn("exit_code=3", evidence[0])

    def test_judge_report_does_not_carry_the_preamble(self) -> None:
        from app.models import AgentState, JobStatus, RepositoryPreflightReport

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.execution_result = _result(PREAMBLE + "SyntaxError: invalid syntax\n")
        report = nodes._fallback_judge(state)
        blob = str(report)
        self.assertNotIn("apt-get install", blob)
        self.assertIn("SyntaxError", blob)
