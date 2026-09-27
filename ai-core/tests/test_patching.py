import subprocess
import tempfile
import unittest
from pathlib import Path

from app.agents import patching


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


if __name__ == "__main__":
    unittest.main()
