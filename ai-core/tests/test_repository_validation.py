import unittest
from unittest.mock import patch

from app.agents.nodes import critic_node, judge_node, planner_node, refiner_node
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.repository.preflight import _normalize_github_url
from app.sandbox.docker_runner import _sandbox_result_from_response
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
        self.assertEqual(queue.payloads[0]["taskId"], state.job_id)
        self.assertEqual(queue.payloads[0]["repositoryUrl"], "https://github.com/CodeReferee-Team/codereferee-AI")
        self.assertEqual(queue.payloads[0]["branch"], "main")

    def test_process_next_repository_validation_dequeues_and_runs_preflight_failure(self) -> None:
        class FakeQueue:
            def __init__(self) -> None:
                self.payload = {
                    "taskId": "job-queue",
                    "repositoryUrl": "https://github.com/example/missing",
                    "branch": None,
                    "commitSha": None,
                    "submittedAt": "2026-05-26T10:00:00",
                }

            def dequeue(self, *, block=False, timeout=0):
                self.block = block
                self.timeout = timeout
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
        queue = FakeQueue()
        with patch("app.workflow.repository_validation.repository_preflight_runner.run", return_value=fake_report), patch(
            "app.workflow.repository_validation.sandbox_runner.run_repository"
        ) as sandbox_run:
            state = process_next_repository_validation(queue=queue, block=True, timeout=0)

        sandbox_run.assert_not_called()
        self.assertTrue(queue.block)
        self.assertEqual(queue.timeout, 0)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.job_id, "job-queue")
        self.assertEqual(state.request_id, "job-queue")
        self.assertEqual(state.status, JobStatus.failed)
        self.assertIn("Queue: payload schema=server", state.events)
        self.assertIn("Queue: repository validation dequeued", state.events)
        self.assertIn("Preflight: failed", state.events)
        self.assertFalse(state.metrics["sandbox_executed"])

    def test_process_next_repository_validation_runs_sandbox_after_preflight_passes(self) -> None:
        class FakeQueue:
            def dequeue(self, *, block=False, timeout=0):
                return {
                    "taskId": "job-pass",
                    "repositoryUrl": "https://github.com/example/project",
                    "branch": "main",
                    "commitSha": None,
                    "submittedAt": "2026-05-26T10:00:00",
                }

        fake_report = RepositoryPreflightReport(
            repository_url="https://github.com/example/project.git",
            cloneable=True,
            executable=True,
            resolved_commit_sha="a" * 40,
            reason="Repository ref is reachable.",
            evidence=["reachable"],
        )
        fake_result = SandboxResult(exit_code=0, stdout="ok", duration_ms=100)

        with patch("app.workflow.repository_validation.repository_preflight_runner.run", return_value=fake_report), patch(
            "app.workflow.repository_validation.sandbox_runner.run_repository", return_value=fake_result
        ) as sandbox_run:
            state = process_next_repository_validation(queue=FakeQueue(), block=True, timeout=0)

        self.assertIsNotNone(state)
        assert state is not None
        sandbox_run.assert_called_once_with("https://github.com/example/project.git", branch="main", commit_sha=None)
        self.assertIn("Preflight: passed", state.events)
        self.assertTrue(state.metrics["preflight_passed"])
        self.assertTrue(state.metrics["sandbox_executed"])

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

    def test_sandbox_http_response_exposes_server_smoke(self) -> None:
        result = _sandbox_result_from_response(
            '{"exitCode":0,"durationMillis":10,"serverStarted":true,"serverUrl":"http://127.0.0.1:3000/",'
            '"httpStatus":200,"browserLoaded":true,"pageTitle":"Demo","runCommand":["npm","run","start"]}',
            started_at=0,
        )
        self.assertTrue(result.server_started)
        self.assertEqual(result.server_url, "http://127.0.0.1:3000/")
        self.assertEqual(result.http_status, 200)
        self.assertTrue(result.browser_loaded)
        self.assertEqual(result.page_title, "Demo")
        self.assertEqual(result.run_command, ["npm", "run", "start"])

    def test_normalize_github_url_accepts_public_https_repo(self) -> None:
        self.assertEqual(
            _normalize_github_url("https://github.com/CodeReferee-Team/codereferee-AI"),
            "https://github.com/CodeReferee-Team/codereferee-AI.git",
        )
        self.assertIsNone(_normalize_github_url("git@github.com:CodeReferee-Team/codereferee-AI.git"))


if __name__ == "__main__":
    unittest.main()
