"""카오스 설정 결함의 edits가 git apply가 먹는 diff로 렌더되는지.

핵심 불변식: 생성한 patch_diff는 sandbox의 strict `git apply`(docker_runner.py,
deploy_repository.py 둘 다)에서 실패 없이 적용돼야 한다. LLM에 diff 형식을 맡기지
않는 이유가 이것이므로, 실제 git에 적용해 본다.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from app.agents import source_context
from app.agents.evidence import build_refiner_evidence
from app.agents.patching import apply_edits, build_diff
from app.models import AgentState, JobStatus, SandboxResult
from app.workflow import repository_validation as workflow

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


class ReplicasEditTests(unittest.TestCase):
    """main의 줄 단위 편집(patching.apply_edits)으로 같은 수정이 나오는지 본다."""

    def _diff(self, edits: list[dict]) -> str | None:
        outcome = apply_edits({PATH: VALIDATION_YAML}, edits)
        return build_diff({PATH: VALIDATION_YAML}, outcome.patched) if outcome.patched else None

    def test_replicas_edit_applies_cleanly(self) -> None:
        diff = self._diff([{"path": PATH, "find": ["  replicas: 1"], "replace": ["  replicas: 2"]}])
        self.assertIsNotNone(diff)
        result = _apply_in_temp_repo(diff, PATH, VALIDATION_YAML)
        self.assertIn("replicas: 2", result)
        self.assertNotIn("replicas: 1\n", result)

    def test_find_not_present_is_rejected(self) -> None:
        outcome = apply_edits({PATH: VALIDATION_YAML}, [{"path": PATH, "find": ["  replicas: 9"], "replace": ["x"]}])
        self.assertFalse(outcome.patched)
        self.assertIn(f"edit_anchor_not_found:{PATH}", outcome.rejected)

    def test_unknown_path_is_rejected(self) -> None:
        outcome = apply_edits({PATH: VALIDATION_YAML}, [{"path": "k8s/deploy.yaml", "find": ["x"], "replace": ["y"]}])
        self.assertFalse(outcome.patched)

    def test_noop_replace_produces_no_diff(self) -> None:
        self.assertFalse(self._diff([{"path": PATH, "find": ["  replicas: 1"], "replace": ["  replicas: 1"]}]))


def _chaos_failed_state() -> AgentState:
    state = AgentState(job_id="c", repository_url="https://github.com/o/r.git", status=JobStatus.failed)
    state.execution_result = SandboxResult(
        exit_code=1,
        chaos_observation={"recovered": True, "recovery_seconds": 125.9, "target_configuration": {"replicas": 1}},
    )
    state.judge_report = {"status": "Fail", "reason_category": "chaos_recovery_exceeds_expected_bound"}
    return state


class ChaosSourceContextTests(unittest.TestCase):
    """카오스 실패 로그에는 경로가 없다. 검증 설정을 Refiner에 보여줘야 replicas를 고칠 수 있다."""

    def test_chaos_failure_attaches_validation_config(self) -> None:
        state = _chaos_failed_state()
        with mock.patch.object(source_context, "collect", return_value={PATH: VALIDATION_YAML}) as collect:
            workflow.attach_source_files(state)
        requested = collect.call_args.args[1]
        self.assertEqual(requested[: len(source_context.CHAOS_CONFIG_CANDIDATES)], list(source_context.CHAOS_CONFIG_CANDIDATES))
        self.assertEqual(state.source_files, {PATH: VALIDATION_YAML})

    def test_refiner_evidence_carries_chaos_configuration(self) -> None:
        state = _chaos_failed_state()
        state.source_files = {PATH: VALIDATION_YAML}
        packet = build_refiner_evidence(state)
        self.assertEqual(packet["chaos"]["replicas"], 1)
        self.assertIn(PATH, packet["source_files"])


if __name__ == "__main__":
    unittest.main()
