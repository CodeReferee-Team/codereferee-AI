"""정상 레포 42개를 돌려 나온 오판을 고정한다(2026-10-20 실측, docs/sandbox.md)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.sandbox import docker_runner


def _run_script(files: dict[str, str], tag: str | None = None, executable: tuple[str, ...] = ()) -> tuple[dict, str]:
    root = tempfile.mkdtemp()
    try:
        source = os.path.join(root, "source")
        os.makedirs(source)
        for path, body in files.items():
            full = os.path.join(source, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(body)
            if path in executable:
                os.chmod(full, 0o755)
        git = ["git", "-C", source, "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        if tag:
            subprocess.run(git + ["tag", "-a", tag, "-m", tag], check=True)
            subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "after release"], check=True)
            sha = subprocess.run(git[:3] + ["rev-parse", f"{tag}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()
        script = docker_runner._repository_validation_script(f"file://{source}", None, sha if tag else None)
        script = script.replace("/tmp/repository", os.path.join(root, "clone"))
        completed = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=300)
        line = next(l for l in completed.stdout.splitlines() if l.startswith(docker_runner.RESULT_SENTINEL))
        return json.loads(line[len(docker_runner.RESULT_SENTINEL):]), completed.stdout + completed.stderr
    finally:
        shutil.rmtree(root, ignore_errors=True)


UNITTEST_STYLE = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue({ok})\n\n\nif __name__ == '__main__':\n    unittest.main()\n"


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git과 sh가 필요하다")
class UnittestFallbackTests(unittest.TestCase):
    """micheles/decorator는 테스트를 tests/test.py에 둔다. pytest 기본 규칙(test_*.py)에 안 잡힌다."""

    def test_unittest_style_file_that_pytest_misses_is_run(self) -> None:
        result, log = _run_script({"requirements.txt": "", "tests/test.py": UNITTEST_STYLE.format(ok="True")})
        self.assertEqual(result["outcome"], "success")
        self.assertIn("pytest collected none; trying unittest", log)

    def test_a_failing_unittest_suite_is_a_test_failure(self) -> None:
        # 대안 실행기를 썼다고 실패를 덮지 않는다.
        result, _ = _run_script({"requirements.txt": "", "tests/test.py": UNITTEST_STYLE.format(ok="False")})
        self.assertEqual(result["failed_step"], "smoke")
        self.assertEqual(result["exit_code"], 1)

    def test_nothing_for_unittest_either_is_still_nothing_to_verify(self) -> None:
        result, _ = _run_script({"requirements.txt": "", "app.py": "x = 1\n", "tests/helpers.py": "y = 2\n"})
        self.assertEqual(result["exit_code"], docker_runner.NOTHING_TO_VERIFY_EXIT_CODE)


@unittest.skipUnless(shutil.which("git") and shutil.which("sh") and shutil.which("npm"), "git, sh, npm이 필요하다")
class NodeTestScriptTests(unittest.TestCase):
    """debug-js/debug의 test는 test:node, 브라우저(karma), lint를 잇는다. 샌드박스에는 브라우저가 없다."""

    def _package(self, scripts: dict[str, str]) -> dict[str, str]:
        return {"package.json": json.dumps({"name": "x", "version": "1.0.0", "scripts": scripts})}

    def test_node_only_script_is_preferred(self) -> None:
        result, _ = _run_script(self._package({"test": "exit 1", "test:node": "node -e 0"}))
        self.assertEqual(result["outcome"], "success")

    def test_failing_node_script_still_fails(self) -> None:
        result, _ = _run_script(self._package({"test": "node -e 0", "test:node": "exit 3"}))
        self.assertEqual(result["failed_step"], "smoke")

    def test_plain_test_script_is_unchanged(self) -> None:
        result, _ = _run_script(self._package({"test": "exit 4"}))
        self.assertEqual(result["exit_code"], 4)


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git과 sh가 필요하다")
class ReleaseTagTests(unittest.TestCase):
    """얕은 clone에는 태그가 없다. setuptools-scm·hatch-vcs가 버전을 0.1.dev1로 계산해
    python-attrs/attrs의 버전 테스트가 깨졌다. 검증하는 커밋에 붙은 태그는 받아 둔다."""

    CHECK = (
        "import subprocess\n\n\n"
        "def test_release_tag_is_visible():\n"
        "    out = subprocess.run(['git', 'describe', '--tags', '--exact-match'], capture_output=True, text=True)\n"
        "    assert out.stdout.strip() == 'v1.0'\n"
    )

    def test_tag_on_the_checked_out_commit_is_fetched(self) -> None:
        result, log = _run_script({"requirements.txt": "", "tests/test_tag.py": self.CHECK}, tag="v1.0")
        self.assertEqual(result["outcome"], "success", log[-1500:])


def _judged(stack: str, failed_step: str, log: str) -> dict:
    state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
    state.preflight_report = RepositoryPreflightReport(repository_url="https://github.com/o/r", cloneable=True, executable=True)
    state.execution_result = SandboxResult(
        exit_code=1, stderr=log, sandbox_report={"detected_stack": stack, "failed_step": failed_step}
    )
    return nodes._fallback_judge(state)


class DockerWordTests(unittest.TestCase):
    """google/gson이 docker_build_failed로 나갔다. 로그의 host.docker.internal(JVM 프록시 설정)에 걸렸다."""

    LOG = "Picked up JAVA_TOOL_OPTIONS: -Dhttp.nonProxyHosts=localhost|host.docker.internal\n[ERROR] Tests run: 3, Failures: 1\n"

    def test_docker_word_in_a_test_step_log_is_not_a_docker_build_failure(self) -> None:
        self.assertNotEqual(_judged("maven", "smoke", self.LOG)["reason_category"], "docker_build_failed")

    def test_log_only_result_keeps_the_old_fallback(self) -> None:
        # 구조화 결과가 없는 경로(2층, 옛 응답)는 그대로 둔다. 이 경로의 이미지 빌드 실패를 잡던 규칙이다.
        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
        state.preflight_report = RepositoryPreflightReport(repository_url="https://github.com/o/r", cloneable=True, executable=True)
        state.execution_result = SandboxResult(exit_code=1, stderr="docker build failed: step 3/7")
        self.assertEqual(nodes._fallback_judge(state)["reason_category"], "docker_build_failed")


def _mvnw(exit_code: int, tests: int | None = None, failures: int = 0) -> str:
    """mvnw 대역. 네트워크 없이 surefire가 남기는 JUnit XML만 흉내 낸다."""
    report = ""
    if tests is not None:
        report = (
            "mkdir -p core/target/surefire-reports\n"
            f"echo '<testsuite name=\"T\" tests=\"{tests}\" failures=\"{failures}\" errors=\"0\" skipped=\"0\"/>'"
            " > core/target/surefire-reports/TEST-T.xml\n"
        )
    return f"#!/bin/sh\n{report}exit {exit_code}\n"


@unittest.skipUnless(shutil.which("git") and shutil.which("sh") and shutil.which("python3"), "git, sh, python3가 필요하다")
class JvmTestReportTests(unittest.TestCase):
    """종료 코드만으로는 "테스트가 0건"과 "테스트는 통과했는데 다른 모듈이 깨짐"을 가를 수 없다.
    google/gson은 gson 모듈 테스트가 전부 통과한 뒤 test-jpms 모듈 컴파일에서 떨어졌다."""

    def _run(self, mvnw: str, declared: bool = False) -> tuple[dict, dict]:
        files = {"pom.xml": "<project/>", "mvnw": mvnw}
        if declared:
            files[".codereferee/validation.yaml"] = "test: ./mvnw -B test\n"
        result, _ = _run_script(files, executable=("mvnw",))
        sandbox = SandboxResult(exit_code=result["exit_code"], stderr="[ERROR] build failure", sandbox_report=result)
        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
        state.preflight_report = RepositoryPreflightReport(repository_url="https://github.com/o/r", cloneable=True, executable=True)
        state.execution_result = sandbox
        return result, nodes._fallback_judge(state)

    def test_reports_are_summarized_in_the_result(self) -> None:
        result, _ = self._run(_mvnw(0, tests=2))
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["test_report"], {"files": 1, "tests": 2, "failed": 0})

    def test_build_that_ran_no_tests_is_nothing_to_verify(self) -> None:
        result, _ = self._run(_mvnw(0))
        self.assertEqual(result["exit_code"], docker_runner.NOTHING_TO_VERIFY_EXIT_CODE)

    def test_build_failure_after_all_tests_passed_is_not_a_code_defect(self) -> None:
        result, judge = self._run(_mvnw(1, tests=3))
        self.assertEqual(result["failed_step"], "smoke")
        self.assertEqual(judge["reason_category"], nodes.ENVIRONMENT_LIMIT_CATEGORY)

    def test_failing_tests_are_still_a_code_defect(self) -> None:
        _, judge = self._run(_mvnw(1, tests=3, failures=1))
        self.assertEqual(judge["status"], "Fail")
        self.assertNotEqual(judge["reason_category"], nodes.ENVIRONMENT_LIMIT_CATEGORY)

    def test_build_that_failed_before_any_test_is_still_a_code_defect(self) -> None:
        # 단일 모듈의 컴파일 오류는 테스트 보고서를 남기지 않는다.
        _, judge = self._run(_mvnw(1))
        self.assertNotEqual(judge["reason_category"], nodes.ENVIRONMENT_LIMIT_CATEGORY)

    def test_declared_command_failure_stays_a_code_defect(self) -> None:
        # 레포가 알려준 명령이 깨졌다면 추측이 틀린 것이 아니다.
        _, judge = self._run(_mvnw(1, tests=3), declared=True)
        self.assertNotEqual(judge["reason_category"], nodes.ENVIRONMENT_LIMIT_CATEGORY)


if __name__ == "__main__":
    unittest.main()
