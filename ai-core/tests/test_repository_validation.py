import unittest
from unittest.mock import patch

from app.agents.nodes import critic_node, judge_node, planner_node, refiner_node
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.repository.preflight import _normalize_github_url
from app.workflow.repository_validation import (
    enqueue_repository_validation,
    process_next_repository_validation,
    to_response,
)
from app.models import RepositoryValidationRequest


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


    def test_enqueue_repository_validation_pushes_redis_payload(self) -> None:
        class FakeQueue:
            def __init__(self) -> None:
                self.payloads = []

            def enqueue(self, payload):
                self.payloads.append(payload)
                return len(self.payloads)

        queue = FakeQueue()
        state = enqueue_repository_validation(
            RepositoryValidationRequest(
                repository_url="https://github.com/CodeReferee-Team/codereferee-AI",
                branch="main",
                request_id="req-queue",
            ),
            queue=queue,
        )
        self.assertEqual(state.status, JobStatus.queued)
        self.assertEqual(len(queue.payloads), 1)
        self.assertEqual(queue.payloads[0]["type"], "repository_validation")
        self.assertEqual(queue.payloads[0]["job_id"], state.job_id)
        self.assertEqual(queue.payloads[0]["request_id"], "req-queue")

    def test_process_next_repository_validation_dequeues_and_runs_preflight_failure(self) -> None:
        class FakeQueue:
            def __init__(self) -> None:
                self.payload = {
                    "type": "repository_validation",
                    "job_id": "job-queue",
                    "request_id": "req-queue",
                    "repository_url": "https://github.com/example/missing",
                    "branch": None,
                    "commit_sha": None,
                }

            def dequeue(self):
                payload = self.payload
                self.payload = None
                return payload

        fake_report = RepositoryPreflightReport(
            repository_url="https://github.com/example/missing.git",
            cloneable=False,
            executable=False,
            reason="Repository or requested ref is not reachable.",
            evidence=["not found"],
        )
        with patch("app.workflow.repository_validation.repository_preflight_runner.run", return_value=fake_report):
            state = process_next_repository_validation(queue=FakeQueue())

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.job_id, "job-queue")
        self.assertEqual(state.request_id, "req-queue")
        self.assertEqual(state.status, JobStatus.failed)
        self.assertIn("Queue: repository validation dequeued", state.events)

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
