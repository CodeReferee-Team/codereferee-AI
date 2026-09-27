import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from app.agents import patching
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.sandbox import docker_runner
from app.workflow import repository_validation


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class PatchGuardTests(unittest.TestCase):
    """sandbox로 보내기 전에 거르는 규칙. sandbox 실행은 비싸고, 위험한 패치는 아예 실행하면 안 된다."""

    def test_empty_diff_is_rejected(self) -> None:
        verdict = patching.inspect_diff("")
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_empty")

    def test_diff_over_size_cap_is_rejected(self) -> None:
        huge = "--- a/x\n+++ b/x\n" + "+line\n" * 200_000
        verdict = patching.inspect_diff(huge)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_too_large")

    def test_patch_touching_ci_config_is_rejected(self) -> None:
        diff = "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-on: push\n+on: []\n"
        verdict = patching.inspect_diff(diff)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_touches_protected_path")

    def test_patch_escaping_the_repository_is_rejected(self) -> None:
        diff = "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-x\n+y\n"
        verdict = patching.inspect_diff(diff)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_escapes_repository")

    def test_ordinary_source_patch_passes_inspection(self) -> None:
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        verdict = patching.inspect_diff(diff)
        self.assertTrue(verdict.accepted, verdict.reason)
        self.assertEqual(verdict.touched_paths, ["app.py"])


class GitApplyCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", "app.py")
        _git(self.repo, "commit", "-qm", "init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_applicable_patch_passes(self) -> None:
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        verdict = patching.check_applies(diff, self.repo)
        self.assertTrue(verdict.accepted, verdict.reason)

    def test_patch_against_different_content_fails_with_reason(self) -> None:
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-nonexistent line\n+x = 2\n"
        verdict = patching.check_applies(diff, self.repo)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_does_not_apply")
        self.assertTrue(verdict.reason)  # 재생성 요청에 붙일 오류 메시지가 있어야 한다

    def test_patch_for_missing_file_fails(self) -> None:
        diff = "--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-a\n+b\n"
        verdict = patching.check_applies(diff, self.repo)
        self.assertFalse(verdict.accepted)


class PatchRerunTests(unittest.TestCase):
    """B2: 패치를 적용한 상태로 sandbox를 다시 돌린다."""

    def test_script_applies_patch_before_validation(self) -> None:
        script = docker_runner._repository_validation_script(
            "https://github.com/o/r", None, None, with_patch=True
        )
        self.assertIn(f"git apply --whitespace=nowarn /workspace/{docker_runner.PATCH_FILENAME}", script)
        self.assertIn(f"exit {docker_runner.PATCH_APPLY_EXIT_CODE}", script)
        # 패치 적용은 clone/checkout 다음, 스택 검출 이전이어야 한다.
        self.assertLess(script.index("git apply"), script.index("detecting project stack"))

    def test_script_without_patch_has_no_apply_step(self) -> None:
        script = docker_runner._repository_validation_script("https://github.com/o/r", None, None)
        self.assertNotIn("git apply", script)

    def test_external_sandbox_rejects_patch_rerun(self) -> None:
        runner = docker_runner.SandboxRunner()
        runner.settings = runner.settings.model_copy(update={"sandbox_base_url": "http://sandbox.internal"})
        result = runner.run_repository("https://github.com/o/r", patch_diff="--- a/x\n+++ b/x\n")
        self.assertEqual(result.infra_error, "sandbox_patch_unsupported")

    def test_rerun_records_result_and_passes_the_patch(self) -> None:
        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.refiner_report = {"patch_diff": "--- a/app.py\n+++ b/app.py\n"}
        with mock.patch.object(
            repository_validation.sandbox_runner, "run_repository", return_value=SandboxResult(exit_code=0)
        ) as run:
            repository_validation._rerun_with_patch(state)
        self.assertEqual(run.call_args.kwargs["patch_diff"], state.refiner_report["patch_diff"])
        self.assertTrue(state.metrics["patch_rerun"]["passed"])
        self.assertTrue(state.metrics["patch_rerun"]["patch_applied"])

    def test_rerun_marks_patch_apply_failure_separately(self) -> None:
        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.refiner_report = {"patch_diff": "--- a/app.py\n+++ b/app.py\n"}
        failure = SandboxResult(exit_code=docker_runner.PATCH_APPLY_EXIT_CODE)
        with mock.patch.object(repository_validation.sandbox_runner, "run_repository", return_value=failure):
            repository_validation._rerun_with_patch(state)
        self.assertFalse(state.metrics["patch_rerun"]["patch_applied"])
        self.assertFalse(state.metrics["patch_rerun"]["passed"])

    def test_rerun_is_skipped_unless_the_patch_was_verified(self) -> None:
        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.refiner_report = {"patch_diff": "--- a/app.py\n+++ b/app.py\n"}
        self.assertFalse(repository_validation._patch_is_applicable(state))
        state.metrics["patch_check"] = {"applies": False}
        self.assertFalse(repository_validation._patch_is_applicable(state))
        state.metrics["patch_check"] = {"applies": True}
        self.assertTrue(repository_validation._patch_is_applicable(state))


if __name__ == "__main__":
    unittest.main()
