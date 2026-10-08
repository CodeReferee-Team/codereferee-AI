"""Refiner가 고른 edits를 git apply가 먹는 diff로 렌더하는지.

핵심 불변식: 생성한 patch_diff는 sandbox의 strict `git apply`(docker_runner.py,
deploy_repository.py 둘 다)에서 실패 없이 적용돼야 한다. LLM에 diff 형식을 맡기지
않는 이유가 이것이므로, 실제 git에 적용해 본다.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.agents import nodes

VALIDATION_YAML = (
    "# CodeReferee 검증 설정. 복원력 결함: replicas 1 — 그 한 대가 죽으면 전면 중단.\n"
    "version: 1\n"
    "service:\n"
    "  port: 8000\n"
    "  replicas: 1\n"
    "  healthPath: /health\n"
)

PATH = ".codereferee/validation.yaml"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )


def _apply_in_temp_repo(diff: str, path: str, content: str) -> str:
    """임시 git 레포에 원본을 커밋하고 diff를 적용한 뒤 결과 파일을 돌려준다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _git(["init", "-q"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-qm", "init"], repo)
        patch_file = repo / "fix.patch"
        patch_file.write_text(diff, encoding="utf-8")
        # sandbox와 동일한 호출: strict check 후 apply.
        _git(["apply", "--check", str(patch_file)], repo)
        _git(["apply", str(patch_file)], repo)
        return target.read_text(encoding="utf-8")


class BuildPatchDiffTests(unittest.TestCase):
    def test_replicas_edit_applies_cleanly(self) -> None:
        edits = [{"path": PATH, "find": "replicas: 1", "replace": "replicas: 2"}]
        diff = nodes._build_patch_diff_from_edits(edits, {PATH: VALIDATION_YAML}, [])
        self.assertIsNotNone(diff)
        self.assertIn(f"diff --git a/{PATH} b/{PATH}", diff)
        result = _apply_in_temp_repo(diff, PATH, VALIDATION_YAML)
        self.assertIn("replicas: 2", result)
        self.assertNotIn("replicas: 1", result)

    def test_find_not_present_is_skipped(self) -> None:
        edits = [{"path": PATH, "find": "replicas: 9", "replace": "replicas: 2"}]
        self.assertIsNone(nodes._build_patch_diff_from_edits(edits, {PATH: VALIDATION_YAML}, []))

    def test_unknown_path_is_skipped(self) -> None:
        edits = [{"path": "k8s/deploy.yaml", "find": "x", "replace": "y"}]
        self.assertIsNone(nodes._build_patch_diff_from_edits(edits, {PATH: VALIDATION_YAML}, []))

    def test_noop_replace_produces_no_diff(self) -> None:
        edits = [{"path": PATH, "find": "replicas: 1", "replace": "replicas: 1"}]
        self.assertIsNone(nodes._build_patch_diff_from_edits(edits, {PATH: VALIDATION_YAML}, []))


class GithubOwnerRepoTests(unittest.TestCase):
    def test_parses_https_github(self) -> None:
        self.assertEqual(
            nodes._github_owner_repo("https://github.com/CodeReferee-Team/codereferee-chaos-demo"),
            ("CodeReferee-Team", "codereferee-chaos-demo"),
        )

    def test_strips_dot_git(self) -> None:
        self.assertEqual(nodes._github_owner_repo("https://github.com/o/r.git"), ("o", "r"))

    def test_rejects_non_github(self) -> None:
        self.assertIsNone(nodes._github_owner_repo("https://gitlab.com/o/r"))
        self.assertIsNone(nodes._github_owner_repo("http://github.com/o/r"))
        self.assertIsNone(nodes._github_owner_repo("https://github.com/only-owner"))


if __name__ == "__main__":
    unittest.main()
