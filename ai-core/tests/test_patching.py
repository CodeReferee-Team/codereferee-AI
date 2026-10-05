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


def _applies(diff: str, repo: Path) -> bool:
    """생성한 diff가 실제로 적용되는지. 적용 가능성은 sandbox도 patch 단계에서 확인한다."""
    done = subprocess.run(["git", "apply", "--check", "-"], cwd=repo, input=diff, text=True, capture_output=True)
    return done.returncode == 0


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
        self.assertTrue(_applies(diff, self.repo))

    def test_unchanged_file_produces_no_diff(self) -> None:
        diff = patching.build_diff({"calc.py": self.original}, {"calc.py": self.original})
        self.assertEqual(diff, "")

    def test_file_we_never_showed_is_ignored(self) -> None:
        # 보여주지 않은 파일의 내용은 지어낸 것이다.
        diff = patching.build_diff({"calc.py": self.original}, {"secret.py": "x = 1\n"})
        self.assertEqual(diff, "")

    def test_missing_trailing_newline_still_applies(self) -> None:
        diff = patching.build_diff({"calc.py": self.original}, {"calc.py": "def add(a, b):\n    return a + b"})
        self.assertTrue(_applies(diff, self.repo))

    def test_multiple_files_are_combined_into_one_diff(self) -> None:
        (self.repo / "util.py").write_text("v = 1\n", encoding="utf-8")
        _git(self.repo, "add", "util.py")
        _git(self.repo, "commit", "-qm", "util")
        diff = patching.build_diff(
            {"calc.py": self.original, "util.py": "v = 1\n"},
            {"calc.py": "def add(a, b):\n    return a + b\n", "util.py": "v = 2\n"},
        )
        self.assertEqual(sorted(patching.touched_paths(diff)), ["calc.py", "util.py"])
        self.assertTrue(_applies(diff, self.repo))


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
        # 편집 방식에서도 큰 삭제는 막는다. 모델이 넓은 구간을 지우라고 할 수는 있다.
        from app.agents import nodes
        from app.models import AgentState, JobStatus

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.source_files = dict(self.original)
        doomed = [f"line {i}" for i in range(40, 80)]
        state.refiner_report = {"edits": [{"path": "mod.py", "find": doomed, "replace": []}]}
        nodes._diff_from_edits(state)
        nodes._record_patch_inspection(state)
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


class ApplyEditsTests(unittest.TestCase):
    """치환은 우리가 한다. 모델은 바꿀 줄만 지목한다."""

    def setUp(self) -> None:
        self.original = {"calc.py": "def add(a, b):\n    return a - b\n\ndef sub(a, b):\n    return a - b\n"}

    def test_unique_anchor_is_replaced(self) -> None:
        out = patching.apply_edits(
            self.original, [{"path": "calc.py", "find": ["def add(a, b):", "    return a - b"],
                             "replace": ["def add(a, b):", "    return a + b"]}]
        )
        self.assertEqual(out.rejected, [])
        self.assertIn("return a + b", out.patched["calc.py"])
        # 다른 함수는 그대로다.
        self.assertIn("def sub(a, b):\n    return a - b", out.patched["calc.py"])

    def test_ambiguous_anchor_is_refused(self) -> None:
        # "    return a - b"는 두 번 나온다. 어디를 말하는지 알 수 없다.
        out = patching.apply_edits(self.original, [{"path": "calc.py", "find": ["    return a - b"], "replace": ["x"]}])
        self.assertEqual(out.patched, {})
        self.assertEqual(out.rejected, ["edit_anchor_ambiguous:calc.py"])

    def test_missing_anchor_is_refused(self) -> None:
        out = patching.apply_edits(self.original, [{"path": "calc.py", "find": ["not here"], "replace": ["x"]}])
        self.assertEqual(out.rejected, ["edit_anchor_not_found:calc.py"])

    def test_unknown_path_is_refused(self) -> None:
        out = patching.apply_edits(self.original, [{"path": "other.py", "find": ["a"], "replace": ["b"]}])
        self.assertEqual(out.rejected, ["edit_path_unknown:other.py"])

    def test_empty_replace_deletes_the_lines(self) -> None:
        out = patching.apply_edits(
            self.original, [{"path": "calc.py", "find": ["def sub(a, b):", "    return a - b"], "replace": []}]
        )
        self.assertNotIn("def sub", out.patched["calc.py"])

    def test_edits_cannot_touch_lines_they_did_not_list(self) -> None:
        # 전문 방식에서 모델이 뒤를 잘라먹어 멀쩡한 코드가 지워졌다. 편집 방식에서는 불가능하다.
        out = patching.apply_edits(
            self.original, [{"path": "calc.py", "find": ["def add(a, b):"], "replace": ["def add(a, b, c=0):"]}]
        )
        diff = patching.build_diff(self.original, out.patched)
        self.assertTrue(patching.inspect_rewrite(diff, self.original).accepted)
        self.assertEqual(len([l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]), 1)

    def test_one_bad_edit_does_not_block_a_good_one(self) -> None:
        out = patching.apply_edits(
            self.original,
            [
                {"path": "nope.py", "find": ["a"], "replace": ["b"]},
                {"path": "calc.py", "find": ["def add(a, b):"], "replace": ["def add(a, b, c=0):"]},
            ],
        )
        self.assertEqual(out.rejected, ["edit_path_unknown:nope.py"])
        self.assertIn("def add(a, b, c=0):", out.patched["calc.py"])


class PatchCheckReasonTests(unittest.TestCase):
    """거부 이유는 덮이지 않아야 한다. patch_absent로 덮으면 왜 거부됐는지 사라진다."""

    def test_specific_rejection_survives_the_inspection_step(self) -> None:
        from app.agents import nodes
        from app.models import AgentState, JobStatus

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.source_files = {"calc.py": "x = 1\n"}
        state.refiner_report = {"edits": [{"path": "calc.py", "find": ["nope"], "replace": ["y"]}]}
        nodes._diff_from_edits(state)
        nodes._record_patch_inspection(state)
        self.assertEqual(state.metrics["patch_check"]["reason_code"], "edits_not_applicable")

    def test_absent_patch_still_reports_patch_absent(self) -> None:
        from app.agents import nodes
        from app.models import AgentState, JobStatus

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.refiner_report = {"summary": "nothing to fix"}
        nodes._record_patch_inspection(state)
        self.assertEqual(state.metrics["patch_check"]["reason_code"], "patch_absent")


class EditPathNormalisationTests(unittest.TestCase):
    """모델은 로그에서 본 형태로 경로를 쓴다. ./src/a.py, /tmp/repository/src/a.py 등."""

    def setUp(self) -> None:
        self.original = {"src/iniconfig/__init__.py": "def broken(:\n    pass\n"}

    def test_dot_slash_prefix_resolves(self) -> None:
        out = patching.apply_edits(
            self.original,
            [{"path": "./src/iniconfig/__init__.py", "find": ["def broken(:"], "replace": ["def broken():"]}],
        )
        self.assertEqual(out.rejected, [])
        self.assertIn("def broken():", out.patched["src/iniconfig/__init__.py"])

    def test_container_prefix_resolves(self) -> None:
        out = patching.apply_edits(
            self.original,
            [{"path": "/tmp/repository/src/iniconfig/__init__.py", "find": ["def broken(:"], "replace": ["ok"]}],
        )
        self.assertEqual(out.rejected, [])

    def test_a_genuinely_unknown_path_is_still_refused(self) -> None:
        out = patching.apply_edits(self.original, [{"path": "./other.py", "find": ["x"], "replace": ["y"]}])
        self.assertEqual(out.rejected, ["edit_path_unknown:./other.py"])
