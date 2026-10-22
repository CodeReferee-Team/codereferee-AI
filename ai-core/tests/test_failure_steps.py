import json
import os
import shutil
import subprocess
import tempfile
import unittest

from app.agents import nodes
from app.agents.schemas import REASON_CATEGORIES
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.sandbox import docker_runner


def _state(exit_code: int, failed_step: str, declared: bool = False) -> AgentState:
    state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
    state.preflight_report = RepositoryPreflightReport(
        repository_url="https://github.com/o/r", cloneable=True, executable=True
    )
    state.execution_result = SandboxResult(
        exit_code=exit_code,
        stderr="*** Error compiling './app.py'...\nSyntaxError: invalid syntax\n",
        sandbox_report={"detected_stack": "python", "failed_step": failed_step, "verification_declared": declared},
    )
    return state


class CompileStepJudgeTests(unittest.TestCase):
    """문법 오류는 테스트 실패와 다른 원인이다. 로그가 아니라 단계로 가른다."""

    def test_compile_failed_is_a_canonical_category(self) -> None:
        self.assertIn("compile_failed", REASON_CATEGORIES)

    def test_compile_step_failure_is_a_compile_failure(self) -> None:
        report = nodes._fallback_judge(_state(1, "compile"))
        self.assertEqual(report["status"], "Fail")
        self.assertEqual(report["reason_category"], "compile_failed")

    def test_oom_during_compile_is_still_an_environment_limit(self) -> None:
        report = nodes._fallback_judge(_state(137, "compile"))
        self.assertEqual(report["reason_category"], nodes.ENVIRONMENT_LIMIT_CATEGORY)

    def test_syntax_error_text_in_smoke_is_not_called_a_compile_failure(self) -> None:
        # 테스트가 SyntaxError를 내는 코드를 검사할 수 있다. 로그 문구로는 컴파일 실패를 단정하지 않는다.
        report = nodes._fallback_judge(_state(1, "smoke", declared=True))
        self.assertNotEqual(report["reason_category"], "compile_failed")


class CompileStepScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = docker_runner._repository_validation_script("https://github.com/o/r", None, None)

    def test_compile_runs_as_its_own_step_before_dependencies(self) -> None:
        compile_at = self.script.index("run_step compile compile_sources")
        self.assertLess(compile_at, self.script.index("run_step dependencies install_dependencies"))

    def test_smoke_no_longer_compiles(self) -> None:
        smoke = self.script[self.script.index("run_smoke_test() {"):]
        smoke = smoke[: smoke.index("\n}\n")]
        self.assertNotIn("compileall", smoke)


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git과 sh가 필요하다")
class ScriptExecutionTests(unittest.TestCase):
    """스크립트를 실제로 돌려 구조화 결과를 본다. 네트워크 없이 로컬 레포를 clone한다."""

    def _run(self, files: dict[str, str]) -> dict:
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        source = os.path.join(root, "source")
        os.makedirs(source)
        for path, body in files.items():
            full = os.path.join(source, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(body)
        git = ["git", "-C", source, "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)

        script = docker_runner._repository_validation_script(f"file://{source}", None, None)
        script = script.replace("/tmp/repository", os.path.join(root, "clone"))
        completed = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=300)
        line = next(l for l in completed.stdout.splitlines() if l.startswith(docker_runner.RESULT_SENTINEL))
        return json.loads(line[len(docker_runner.RESULT_SENTINEL):])

    def test_syntax_error_fails_the_compile_step(self) -> None:
        result = self._run({"requirements.txt": "", "app.py": "def broken(:\n", "tests/test_app.py": ""})
        self.assertEqual(result["failed_step"], "compile")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertNotIn("dependencies", [step["name"] for step in result["steps"]])

    def test_declared_repository_skips_the_compile_step(self) -> None:
        result = self._run({
            "requirements.txt": "",
            "tests/fixtures/py2_sample.py": "print 'legacy'\n",
            ".codereferee/validation.yaml": "test: true\n",
        })
        # 단계 기록은 남지만 아무것도 돌리지 않는다. 깨진 py2 파일이 있어도 통과한다.
        self.assertEqual(result["outcome"], "success")

    def test_tests_directory_without_tests_is_nothing_to_verify(self) -> None:
        # pytest는 모은 테스트가 없으면 5로 끝낸다. 통과도 일반 실패도 아니고 "검증할 것이 없음"이다.
        result = self._run({"requirements.txt": "", "app.py": "x = 1\n", "tests/helpers.py": "y = 2\n"})
        self.assertEqual(result["failed_step"], "smoke")
        self.assertEqual(result["exit_code"], docker_runner.NOTHING_TO_VERIFY_EXIT_CODE)

    # 아래 둘은 BugsInPy 실측에서 나온 오판이다(2026-10-20). 수정 커밋인데도 불합격으로 나갔다.
    def test_intentionally_invalid_test_data_does_not_fail_the_compile_step(self) -> None:
        # psf/black은 tests/data에 일부러 문법이 틀린 파일을 둔다. 포매터의 입력이다.
        result = self._run({
            "requirements.txt": "",
            "app.py": "x = 1\n",
            "tests/data/invalid_input.py": "return outside_function\n",
            "tests/test_app.py": "def test_ok():\n    assert True\n",
        })
        self.assertEqual(result["outcome"], "success")

    def test_tests_inside_the_package_are_found(self) -> None:
        # tqdm은 tests를 패키지 안(tqdm/tests)에 둔다. 최상위 tests가 없다고 "테스트 없음"으로 내면 안 된다.
        result = self._run({
            "requirements.txt": "",
            "pkg/__init__.py": "",
            "pkg/tests/__init__.py": "",
            "pkg/tests/test_pkg.py": "def test_ok():\n    assert True\n",
        })
        self.assertEqual(result["outcome"], "success")

    def test_no_test_files_anywhere_is_still_nothing_to_verify(self) -> None:
        result = self._run({"requirements.txt": "", "app.py": "x = 1\n"})
        self.assertEqual(result["exit_code"], docker_runner.NOTHING_TO_VERIFY_EXIT_CODE)


if __name__ == "__main__":
    unittest.main()
