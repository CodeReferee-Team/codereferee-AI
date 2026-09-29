import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from app.agents import nodes, patching
from app.config import get_settings
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
            _, summary = repository_validation._rerun_with_patch(
                state, str(state.refiner_report["patch_diff"])
            )
        self.assertEqual(run.call_args.kwargs["patch_diff"], state.refiner_report["patch_diff"])
        self.assertTrue(summary["passed"])
        self.assertTrue(summary["patch_applied"])

    def test_rerun_marks_patch_apply_failure_separately(self) -> None:
        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.refiner_report = {"patch_diff": "--- a/app.py\n+++ b/app.py\n"}
        failure = SandboxResult(exit_code=docker_runner.PATCH_APPLY_EXIT_CODE)
        with mock.patch.object(repository_validation.sandbox_runner, "run_repository", return_value=failure):
            _, summary = repository_validation._rerun_with_patch(
                state, str(state.refiner_report["patch_diff"])
            )
        self.assertFalse(summary["patch_applied"])
        self.assertFalse(summary["passed"])

    def test_rerun_is_skipped_unless_the_patch_was_verified(self) -> None:
        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.refiner_report = {"patch_diff": "--- a/app.py\n+++ b/app.py\n"}
        self.assertFalse(repository_validation._patch_is_applicable(state))
        state.metrics["patch_check"] = {"applies": False}
        self.assertFalse(repository_validation._patch_is_applicable(state))
        state.metrics["patch_check"] = {"applies": True}
        self.assertTrue(repository_validation._patch_is_applicable(state))


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
FOLLOW_UP = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 2\n+x = 3\n"


def _failing_state() -> AgentState:
    state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
    state.preflight_report = RepositoryPreflightReport(
        repository_url="https://github.com/o/r", cloneable=True, executable=True
    )
    state.refiner_report = {"patch_diff": DIFF}
    state.metrics["patch_check"] = {"applies": True}
    return state


class PatchRoundTests(unittest.TestCase):
    """B3: 라운드 루프. 상한은 라운드 수와 누적 diff 크기."""

    def setUp(self) -> None:
        self.progress: list[tuple[int | None, int | None]] = []
        # 라운드 루프 테스트는 루프 제어를 본다. LLM을 켜두면 실제 호출이 나간다.
        self._llm_enabled = nodes.llm.enabled
        nodes.llm.enabled = False

    def tearDown(self) -> None:
        nodes.llm.enabled = self._llm_enabled

    def _emit(self, step: str, **kwargs) -> None:
        self.progress.append((kwargs.get("round_"), kwargs.get("max_rounds")))

    def test_passing_rerun_stops_after_one_round(self) -> None:
        state = _failing_state()
        with mock.patch.object(
            repository_validation.sandbox_runner, "run_repository", return_value=SandboxResult(exit_code=0)
        ) as run:
            repository_validation._run_patch_rounds(state, self._emit)
        self.assertEqual(run.call_count, 1)
        rounds = state.metrics["patch_rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertTrue(rounds[0]["passed"])
        self.assertEqual(self.progress, [(1, 3)])

    def test_failing_rerun_feeds_the_next_round_and_accumulates_the_diff(self) -> None:
        state = _failing_state()
        with mock.patch.object(
            repository_validation.sandbox_runner, "run_repository", return_value=SandboxResult(exit_code=1)
        ) as run, mock.patch.object(
            repository_validation, "_next_patch_from_rerun", return_value=(FOLLOW_UP, {"status": "Fail"})
        ), mock.patch.object(
            repository_validation, "_check_applies", return_value=patching.PatchVerdict(True)
        ), mock.patch.object(
            repository_validation, "get_settings", return_value=get_settings().model_copy(
                update={"max_self_healing_retries": 2}
            )
        ):
            repository_validation._run_patch_rounds(state, self._emit)
        self.assertEqual(run.call_count, 2)
        # 2라운드는 누적 diff로 돌아야 한다. 새 패치만 보내면 1라운드 수정이 사라진다.
        self.assertEqual(run.call_args.kwargs["patch_diff"], DIFF + FOLLOW_UP)
        self.assertEqual(state.refiner_report["patch_diff"], DIFF + FOLLOW_UP)
        self.assertEqual(self.progress, [(1, 2), (2, 2)])
        self.assertEqual(state.metrics["patch_rounds"][-1]["stopped"], "round_limit")

    def test_round_stops_when_the_cumulative_diff_exceeds_the_cap(self) -> None:
        state = _failing_state()
        huge = "--- a/big.py\n+++ b/big.py\n" + "+line\n" * 200_000
        with mock.patch.object(
            repository_validation.sandbox_runner, "run_repository", return_value=SandboxResult(exit_code=1)
        ) as run, mock.patch.object(
            repository_validation, "_next_patch_from_rerun", return_value=(huge, {"status": "Fail"})
        ):
            repository_validation._run_patch_rounds(state, self._emit)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(state.metrics["patch_rounds"][0]["stopped"], "patch_too_large")
        self.assertEqual(state.refiner_report["patch_diff"], DIFF)

    def test_round_stops_when_the_accumulated_patch_no_longer_applies(self) -> None:
        state = _failing_state()
        with mock.patch.object(
            repository_validation.sandbox_runner, "run_repository", return_value=SandboxResult(exit_code=1)
        ), mock.patch.object(
            repository_validation, "_next_patch_from_rerun", return_value=(FOLLOW_UP, {"status": "Fail"})
        ), mock.patch.object(
            repository_validation,
            "_check_applies",
            return_value=patching.PatchVerdict(False, "patch_does_not_apply", "context mismatch"),
        ):
            repository_validation._run_patch_rounds(state, self._emit)
        self.assertEqual(state.metrics["patch_rounds"][0]["stopped"], "patch_does_not_apply")
        self.assertEqual(state.refiner_report["patch_diff"], DIFF)

    def test_patch_apply_failure_stops_the_loop(self) -> None:
        state = _failing_state()
        failure = SandboxResult(exit_code=docker_runner.PATCH_APPLY_EXIT_CODE)
        with mock.patch.object(
            repository_validation.sandbox_runner, "run_repository", return_value=failure
        ) as run:
            repository_validation._run_patch_rounds(state, self._emit)
        self.assertEqual(run.call_count, 1)

    def test_rejudging_the_rerun_leaves_the_original_verdict_alone(self) -> None:
        state = _failing_state()
        state.judge_report = {"status": "Fail", "reason_category": "test_failure", "reason": "r", "evidence": ["e"]}
        original = dict(state.judge_report)
        follow_up, verdict = repository_validation._next_patch_from_rerun(
            state, SandboxResult(exit_code=1), {"passed": False}, DIFF
        )
        self.assertEqual(state.judge_report, original)
        self.assertEqual(verdict["status"], "Fail")
        # LLM이 꺼진 기본 설정에서는 규칙 기반 Refiner가 diff를 만들지 않는다. 루프는 여기서 끝난다.
        self.assertIsNone(follow_up)


if __name__ == "__main__":
    unittest.main()


class BuildDiffTests(unittest.TestCase):
    """모델은 고친 파일 전문을 주고, diff는 우리가 만든다. docs/evaluation-design.md 14.5."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        self.original = "def add(a, b):\n    return a - b\n"
        (self.repo / "calc.py").write_text(self.original, encoding="utf-8")
        _git(self.repo, "add", "calc.py")
        _git(self.repo, "commit", "-qm", "init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generated_diff_applies_to_the_repository(self) -> None:
        patched = {"calc.py": "def add(a, b):\n    return a + b\n"}
        diff = patching.build_diff({"calc.py": self.original}, patched)
        verdict = patching.check_applies(diff, self.repo)
        self.assertTrue(verdict.accepted, verdict.reason)

    def test_unchanged_file_produces_no_diff(self) -> None:
        diff = patching.build_diff({"calc.py": self.original}, {"calc.py": self.original})
        self.assertEqual(diff, "")

    def test_file_we_never_showed_is_ignored(self) -> None:
        # 보여주지 않은 파일의 내용은 지어낸 것이다.
        diff = patching.build_diff({"calc.py": self.original}, {"secret.py": "x = 1\n"})
        self.assertEqual(diff, "")

    def test_missing_trailing_newline_still_applies(self) -> None:
        diff = patching.build_diff({"calc.py": self.original}, {"calc.py": "def add(a, b):\n    return a + b"})
        self.assertTrue(patching.check_applies(diff, self.repo).accepted)

    def test_multiple_files_are_combined_into_one_diff(self) -> None:
        (self.repo / "util.py").write_text("v = 1\n", encoding="utf-8")
        _git(self.repo, "add", "util.py")
        _git(self.repo, "commit", "-qm", "util")
        diff = patching.build_diff(
            {"calc.py": self.original, "util.py": "v = 1\n"},
            {"calc.py": "def add(a, b):\n    return a + b\n", "util.py": "v = 2\n"},
        )
        self.assertEqual(sorted(patching.touched_paths(diff)), ["calc.py", "util.py"])
        self.assertTrue(patching.check_applies(diff, self.repo).accepted)


REAL_TRUNCATING_DIFF = '''--- a/documentation/conf.py
+++ b/documentation/conf.py
@@ -48,40 +48,9 @@
 # The full version, including alpha/beta/rc tags.
 release = six_version
 
-# The language for content autogenerated by Sphinx. Refer to documentation
-# for a list of supported languages.
-#language = None
-
-# There are two options for replacing |today|: either, you set today to some
-# non-false value, then it is used:
-#today = ''
-# Else, today_fmt is used as the format for a strftime call.
-#today_fmt = '%B %d, %Y'
-
-# List of patterns, relative to source directory, that match files and
-# directories to ignore when looking for source files.
-exclude_patterns = ["_build"]
-
-# The reST default role (used for this markup: `text`) to use for all documents.
-#default_role = None
-
-# If true, '()' will be appended to :func: etc. cross-reference text.
-#add_function_parentheses = True
-
-# If true, the current module name will be prepended to all description
-# unit titles (such as .. function::).
-#add_module_names = True
-
-# If true, sectionauthor and moduleauthor directives will be shown in the
-# output. They are ignored by default.
-#show_authors = False
-
-# The name of the Pygments (syntax highlighting) style to use.
-pygments_style = "sphinx"
-
-# A list of ignored prefixes for module index sorting.
-#modindex_common_prefix = []
-
+# The language for content autogenerated by Sphinx. Refer to documentation for
+# the list of supported languages.
+language = "en"
 
 # -- Options for HTML output ---------------------------------------------------
 
@@ -216,5 +185,5 @@
 intersphinx_mapping = {"py2" : ("https://docs.python.org/2/", None),
                        "py3" : ("https://docs.python.org/3/", None)}
 
-def broken(:
+def broken():
     pass
'''


class RewriteGuardTests(unittest.TestCase):
    """작은 모델은 파일 전문을 재현하라면 뒤를 잘라먹는다. 그 diff는 git apply를 통과한다."""

    def setUp(self) -> None:
        self.original = {"mod.py": "".join(f"line {i}\n" for i in range(100))}

    def test_small_repair_is_accepted(self) -> None:
        patched = {"mod.py": self.original["mod.py"].replace("line 50\n", "line fifty\n")}
        diff = patching.build_diff(self.original, patched)
        self.assertTrue(patching.inspect_rewrite(diff, self.original).accepted)

    def test_truncated_file_is_rejected(self) -> None:
        # 모델이 뒤 40%를 빠뜨린 경우. 유효한 diff지만 멀쩡한 코드를 지운다.
        patched = {"mod.py": "".join(f"line {i}\n" for i in range(60))}
        diff = patching.build_diff(self.original, patched)
        verdict = patching.inspect_rewrite(diff, self.original)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_rewrites_file")

    def test_small_file_gets_an_absolute_floor(self) -> None:
        # 3줄 파일에서 2줄을 고치는 것은 비율로는 크지만 정상적인 수리다.
        original = {"tiny.py": "a\nb\nc\n"}
        diff = patching.build_diff(original, {"tiny.py": "a\nB\nC\n"})
        self.assertTrue(patching.inspect_rewrite(diff, original).accepted)

    def test_refiner_node_drops_a_rewrite(self) -> None:
        from app.agents import nodes
        from app.models import AgentState, JobStatus

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.source_files = dict(self.original)
        state.refiner_report = {"patched_files": {"mod.py": "".join(f"line {i}\n" for i in range(60))}}
        nodes._diff_from_patched_files(state)
        self.assertIsNone(state.refiner_report["patch_diff"])
        self.assertEqual(state.metrics["patch_check"]["reason_code"], "patch_rewrites_file")


class RealWorldRewriteTests(unittest.TestCase):
    """실측 회귀. llama3.1:8b가 낸 패치다. 문법 오류 한 줄을 고치면서 설정 31줄을 지웠고,
    재실행이 통과해 "고쳤다"로 기록됐다. 상한이 20%였을 때 통과했다."""

    def test_the_patch_that_slipped_through_is_now_rejected(self) -> None:
        original = {"documentation/conf.py": "".join(f"line {i}\n" for i in range(221))}
        verdict = patching.inspect_rewrite(REAL_TRUNCATING_DIFF, original)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_rewrites_file")

    def test_the_one_line_syntax_fix_alone_is_accepted(self) -> None:
        # 같은 결함을 국소적으로만 고친 패치는 통과해야 한다.
        original = {"conf.py": "a = 1\n\ndef broken(:\n    pass\n"}
        patched = {"conf.py": "a = 1\n\ndef broken():\n    pass\n"}
        diff = patching.build_diff(original, patched)
        self.assertTrue(patching.inspect_rewrite(diff, original).accepted)
