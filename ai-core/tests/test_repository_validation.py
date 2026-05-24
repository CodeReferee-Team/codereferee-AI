import unittest

from app.agents.nodes import critic_node, judge_node, planner_node, refiner_node
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.repository.preflight import _normalize_github_url
from app.workflow.repository_validation import to_response


class RepositoryValidationTests(unittest.TestCase):
    def test_planner_builds_repository_validation_plan(self) -> None:
        state = AgentState(job_id="test", repository_url="https://github.com/example/project.git")
        result = planner_node(state)
        self.assertTrue(result.validation_plan["objective"].startswith("Validate an existing GitHub repository"))
        self.assertIn("cloneability", result.validation_plan["validation_scope"])

    def test_judge_fails_uncloneable_repository_before_sandbox(self) -> None:
        state = AgentState(
            job_id="test",
            repository_url="https://github.com/example/missing.git",
            preflight_report=RepositoryPreflightReport(
                repository_url="https://github.com/example/missing.git",
                cloneable=False,
                executable=False,
                reason="Repository or requested ref is not reachable.",
                evidence=["not found"],
            ),
        )
        result = judge_node(state)
        self.assertEqual(result.status, JobStatus.failed)
        self.assertEqual(result.error_count, 1)
        self.assertIn("not reachable", result.judge_report["reason"])

    def test_critic_and_refiner_return_remediation_report_not_code(self) -> None:
        state = AgentState(
            job_id="test",
            repository_url="https://github.com/example/project.git",
            preflight_report=RepositoryPreflightReport(
                repository_url="https://github.com/example/project.git",
                cloneable=True,
                executable=True,
                reason="reachable",
            ),
            execution_result=SandboxResult(exit_code=87, stderr="No Gradle wrapper in sandbox image"),
        )
        state = judge_node(state)
        state = critic_node(state)
        state = refiner_node(state)
        self.assertEqual(state.status, JobStatus.failed)
        self.assertIn("patch_guidance", state.refiner_report)
        self.assertIn("recommended_action", state.critic_feedback)

    def test_to_response_exposes_commit_and_metrics(self) -> None:
        state = AgentState(
            job_id="test",
            repository_url="https://github.com/example/project.git",
            resolved_commit_sha="a" * 40,
            metrics={"exit_code": 0, "timed_out": False},
        )
        response = to_response(state, request_id="req-1")
        self.assertEqual(response.request_id, "req-1")
        self.assertEqual(response.commit_sha, "a" * 40)
        self.assertEqual(response.metrics["exit_code"], 0)

    def test_normalize_github_url_accepts_public_https_repo(self) -> None:
        self.assertEqual(
            _normalize_github_url("https://github.com/CodeReferee-Team/codereferee-AI"),
            "https://github.com/CodeReferee-Team/codereferee-AI.git",
        )
        self.assertIsNone(_normalize_github_url("git@github.com:CodeReferee-Team/codereferee-AI.git"))


if __name__ == "__main__":
    unittest.main()
