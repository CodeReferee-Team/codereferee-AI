"""준비 단계(설치·테스트·기동) 실패는 판정 불가다. 안정성만 판정한다(judge-policy 3.0절)."""
import unittest
from unittest.mock import patch

from app.agents import nodes, source_context
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.workflow.repository_validation import process_next_repository_validation

URL = "https://github.com/example/project.git"


def _judged(result: SandboxResult, *, preflight_ok: bool = True) -> AgentState:
    state = AgentState(job_id="t", repository_url=URL)
    state.preflight_report = RepositoryPreflightReport(
        repository_url=URL, cloneable=preflight_ok, executable=preflight_ok, reason="r", evidence=["r"]
    )
    state.execution_result = result if preflight_ok else None
    return nodes.judge_node(state)


class JudgementUnavailableTests(unittest.TestCase):
    def test_failing_tests_mean_stability_was_not_measured(self) -> None:
        state = _judged(SandboxResult(exit_code=1, stderr="FAILED tests/test_x.py",
                                      sandbox_report={"detected_stack": "python", "failed_step": "smoke", "verification_declared": True}))
        self.assertEqual(state.judge_report["reason_category"], "test_failure")  # 사유는 그대로 남긴다
        self.assertEqual(nodes.judgement_unavailable(state), nodes.BASELINE_NOT_ESTABLISHED)

    def test_every_baseline_category_is_a_known_category(self) -> None:
        from app.agents.schemas import REASON_CATEGORIES
        self.assertLessEqual(nodes.BASELINE_FAILURE_CATEGORIES, set(REASON_CATEGORIES))

    def test_timeout_and_service_smoke_are_baseline_failures(self) -> None:
        for result in (SandboxResult(exit_code=None, timed_out=True),
                       SandboxResult(exit_code=0, service_check_attempted=True, server_started=False)):
            with self.subTest(result=result):
                self.assertEqual(nodes.judgement_unavailable(_judged(result)), nodes.BASELINE_NOT_ESTABLISHED)

    def test_preflight_failure_stays_a_fail(self) -> None:
        self.assertIsNone(nodes.judgement_unavailable(_judged(SandboxResult(exit_code=1), preflight_ok=False)))

    def test_pass_is_untouched(self) -> None:
        state = _judged(SandboxResult(exit_code=0, sandbox_report={"detected_stack": "python", "failed_step": "none"}))
        self.assertIsNone(nodes.judgement_unavailable(state))

    def test_environment_limit_keeps_its_own_reason(self) -> None:
        state = _judged(SandboxResult(exit_code=2, sandbox_report={"detected_stack": "python", "failed_step": "smoke"}))
        self.assertEqual(nodes.judgement_unavailable(state), nodes.ENVIRONMENT_LIMIT_CATEGORY)


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._collect = patch.object(source_context, "collect", return_value={})
        self._collect.start()
        self.addCleanup(self._collect.stop)

    def test_failing_baseline_is_error_and_skips_the_patch_loop(self) -> None:
        class FakeQueue:
            def dequeue(self, *, block=False, timeout=0):
                return {"taskId": "job-base", "repositoryUrl": "https://github.com/example/project",
                        "branch": "main", "commitSha": None, "submittedAt": "2026-10-21T10:00:00"}

            def publish(self, event):
                return 1

        report = RepositoryPreflightReport(repository_url=URL, cloneable=True, executable=True,
                                           resolved_commit_sha="a" * 40, reason="reachable", evidence=["reachable"])
        failing = SandboxResult(exit_code=1, duration_ms=100, stderr="FAILED tests/test_x.py::test_a",
                                sandbox_report={"detected_stack": "python", "failed_step": "smoke", "outcome": "failure",
                                                "exit_code": 1, "verification_declared": True, "steps": []})
        with patch("app.workflow.repository_validation.repository_preflight_runner.run", return_value=report), patch(
                "app.workflow.repository_validation.sandbox_runner.run_repository", return_value=failing) as run:
            state = process_next_repository_validation(queue=FakeQueue(), block=True, timeout=0)

        assert state is not None
        self.assertEqual(state.status, JobStatus.error)
        self.assertEqual(state.judge_report["reason_category"], "test_failure")
        self.assertFalse(state.critic_feedback)
        self.assertEqual(run.call_count, 1)  # 재검증 라운드 없음


if __name__ == "__main__":
    unittest.main()
