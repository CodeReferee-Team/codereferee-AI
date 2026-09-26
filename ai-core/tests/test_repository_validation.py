import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.nodes import critic_node, judge_node, planner_node, refiner_node
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.repository.preflight import _normalize_github_url
from app.storage.sqlite_store import SQLitePatchStore
from app.sandbox.docker_runner import _sandbox_result_from_response
from app.workflow.repository_validation import (
    _sre_metrics_from_execution,
    execute_repository_validation,
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

    def test_to_response_exposes_commit_metrics_and_sre_metrics(self) -> None:
        state = AgentState(
            job_id="test",
            repository_url="https://github.com/example/project.git",
            resolved_commit_sha="a" * 40,
            metrics={"exit_code": 0, "timed_out": False},
        )
        state.sre_metrics.sli.availability_percent = 100.0
        response = to_response(state, request_id="req-1")
        self.assertEqual(response.request_id, "req-1")
        self.assertEqual(response.commit_sha, "a" * 40)
        self.assertEqual(response.metrics["exit_code"], 0)
        self.assertEqual(response.sre_metrics.sli.availability_percent, 100.0)

    def test_sqlite_patch_store_records_validation_and_patch_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLitePatchStore(Path(tmpdir) / "codereferee.sqlite3")
            state = AgentState(
                job_id="sqlite-job",
                repository_url="https://github.com/example/project.git",
                branch="main",
                resolved_commit_sha="b" * 40,
                status=JobStatus.failed,
                judge_report={"status": "Fail", "reason_category": "latency_slo_violation"},
                critic_feedback={
                    "issue": "Repository failed SLO validation.",
                    "root_cause": "p95 latency exceeded SLO.",
                },
                refiner_report={
                    "patch_guidance": ["Add timeout budgets."],
                    "risk": "medium",
                },
            )
            run_id = store.save_validation_run(state)
            patch_id = store.save_patch_suggestion(run_id=run_id, state=state)
            self.assertGreater(run_id, 0)
            self.assertGreater(patch_id, 0)

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

    @staticmethod
    def _run_workflow(preflight: RepositoryPreflightReport, sandbox_result: SandboxResult | None) -> AgentState:
        state = AgentState(job_id="infra", repository_url="https://github.com/example/project.git")
        with patch(
            "app.workflow.repository_validation.repository_preflight_runner.run", return_value=preflight
        ), patch(
            "app.workflow.repository_validation.sandbox_runner.run_repository", return_value=sandbox_result
        ), patch("app.workflow.repository_validation.record_validation_artifacts"):
            return execute_repository_validation(state)

    @staticmethod
    def _reachable_preflight() -> RepositoryPreflightReport:
        return RepositoryPreflightReport(
            repository_url="https://github.com/example/project.git",
            cloneable=True,
            executable=True,
            reason="reachable",
        )

    def test_infra_failure_is_error_not_user_code_failure(self) -> None:
        result = SandboxResult(
            exit_code=None,
            stderr="Docker repository sandbox error: cannot connect to the Docker daemon",
            infra_error="docker_daemon_unreachable",
        )
        state = self._run_workflow(self._reachable_preflight(), result)
        self.assertEqual(state.status, JobStatus.error)
        self.assertEqual(state.judge_report, {})
        self.assertEqual(state.critic_feedback, {})
        self.assertEqual(state.refiner_report, {})

    def test_user_code_failure_stays_failed(self) -> None:
        result = SandboxResult(exit_code=1, stderr="pytest: 3 failed")
        state = self._run_workflow(self._reachable_preflight(), result)
        self.assertEqual(state.status, JobStatus.failed)
        self.assertNotEqual(state.judge_report, {})

    def test_preflight_infra_failure_is_error(self) -> None:
        preflight = RepositoryPreflightReport(
            repository_url="https://github.com/example/project.git",
            reason="git is not installed on the AI core host, so repository intake cannot be verified.",
            infra_error="git_not_installed",
        )
        state = self._run_workflow(preflight, None)
        self.assertEqual(state.status, JobStatus.error)
        self.assertEqual(state.judge_report, {})

    def test_chaos_without_baseline_is_error(self) -> None:
        result = SandboxResult(
            exit_code=0,
            duration_ms=5000,
            metrics={"availability": 0.75},
            chaos_observation={"type": "pod_kill", "recovered": True},
        )
        state = self._run_workflow(self._reachable_preflight(), result)
        self.assertEqual(state.status, JobStatus.error)
        self.assertTrue(any("chaos_evidence_missing" in event for event in state.events))

    def test_aborted_chaos_experiment_is_error(self) -> None:
        result = SandboxResult(
            exit_code=0,
            duration_ms=5000,
            metrics={"availability": 0.99, "recovery_seconds": 2.0},
            baseline={"metrics": {"p95_latency_ms": 12.0}},
            chaos_observation={"type": "pod_kill", "recovered": True, "aborted": True},
        )
        state = self._run_workflow(self._reachable_preflight(), result)
        self.assertEqual(state.status, JobStatus.error)
        self.assertTrue(any("chaos_experiment_aborted" in event for event in state.events))

    @staticmethod
    def _chaos_state(job_id: str, **metrics_overrides) -> AgentState:
        metrics = {"availability": 0.99, "error_rate": 0.01, "p95_latency_ms": 20.0, "recovery_seconds": 3.42}
        metrics.update(metrics_overrides.pop("metrics", {}))
        observation = {"type": "pod_kill", "recovered": True}
        observation.update(metrics_overrides.pop("chaos_observation", {}))
        return AgentState(
            job_id=job_id,
            repository_url="https://github.com/example/project.git",
            preflight_report=RepositoryPreflightReport(
                repository_url="https://github.com/example/project.git",
                cloneable=True,
                executable=True,
                reason="reachable",
            ),
            execution_result=SandboxResult(
                exit_code=0,
                duration_ms=5000,
                metrics=metrics,
                chaos_observation=observation,
                baseline={"metrics": {"p95_latency_ms": 12.0}},
                source={"fixture": "fixture-api", "real_execution_observed": True},
            ),
        )

    def test_judge_fails_when_chaos_experiment_never_recovered(self) -> None:
        state = judge_node(self._chaos_state("not-recovered", chaos_observation={"recovered": False}))
        self.assertEqual(state.status, JobStatus.failed)
        self.assertIn("chaos_not_recovered", state.judge_report["reason"])

    def test_judge_fails_when_chaos_downtime_exhausts_error_budget(self) -> None:
        # 99.9% 목표의 월간 허용 불가용 시간은 2,592초다. 그보다 긴 복구는 버짓 소진이다.
        state = judge_node(self._chaos_state("budget-out", metrics={"recovery_seconds": 3000.0}))
        self.assertEqual(state.status, JobStatus.failed)
        self.assertIn("chaos_error_budget_exhausted", state.judge_report["reason"])

    def test_judge_passes_with_warning_when_chaos_burns_significant_budget(self) -> None:
        # 버짓의 20%(518.4초)를 넘지만 소진은 아니다. 판정은 Pass, 경고만 남긴다.
        state = judge_node(self._chaos_state("budget-warn", metrics={"recovery_seconds": 900.0}))
        self.assertEqual(state.status, JobStatus.success)
        self.assertIn("chaos_error_budget_significant_burn", state.metrics["policy_warnings"])

    def test_judge_passes_with_warning_when_latency_degrades_against_baseline(self) -> None:
        state = judge_node(self._chaos_state("latency-warn", metrics={"p95_latency_ms": 2009.0}))
        self.assertEqual(state.status, JobStatus.success)
        self.assertIn("chaos_latency_degraded", state.metrics["policy_warnings"])

    def test_judge_passes_clean_chaos_run_without_warnings(self) -> None:
        state = judge_node(self._chaos_state("clean"))
        self.assertEqual(state.status, JobStatus.success)
        self.assertEqual(state.metrics.get("policy_warnings", []), [])

    def test_judge_fails_measured_slo_violation_without_chaos(self) -> None:
        state = self._chaos_state("slo-violation", metrics={"error_rate": 0.05})
        state.execution_result.chaos_observation = {}
        state = judge_node(state)
        self.assertEqual(state.status, JobStatus.failed)
        self.assertIn("error_rate_slo_violation", state.judge_report["reason"])

    def test_judge_does_not_apply_slo_to_estimated_metrics(self) -> None:
        # 실측값이 없으면 duration_ms가 p95 대용으로 쓰인다. 여기에 SLO를 걸면 느린 빌드가 전부 Fail이 된다.
        state = AgentState(
            job_id="slow-build",
            repository_url="https://github.com/example/project.git",
            preflight_report=RepositoryPreflightReport(
                repository_url="https://github.com/example/project.git",
                cloneable=True,
                executable=True,
                reason="reachable",
            ),
            execution_result=SandboxResult(exit_code=0, duration_ms=60000),
        )
        state = judge_node(state)
        self.assertEqual(state.status, JobStatus.success)

    def test_sre_metrics_prefer_measured_chaos_values_over_estimates(self) -> None:
        state = AgentState(
            job_id="chaos-measured",
            repository_url="https://github.com/example/project.git",
            execution_result=SandboxResult(
                exit_code=0,
                duration_ms=5000,
                metrics={
                    "availability": 0.75,
                    "error_rate": 0.25,
                    "p95_latency_ms": 2009,
                    "recovery_seconds": 3.42,
                },
                chaos_observation={"type": "pod_kill", "recovered": True},
                source={"fixture": "fixture-api", "real_execution_observed": True},
            ),
        )
        sre = _sre_metrics_from_execution(state)
        self.assertEqual(sre.sli.availability_percent, 75.0)
        self.assertEqual(sre.sli.error_rate, 0.25)
        self.assertEqual(sre.sli.p95_latency_ms, 2009.0)
        self.assertEqual(sre.error_budget.observed_error_rate, 0.25)
        self.assertEqual(sre.chaos.scenario, "pod_kill")
        self.assertEqual(sre.chaos.target, "fixture-api")
        self.assertTrue(sre.chaos.recovered)
        self.assertEqual(sre.chaos.recovery_time_sec, 3.42)

    def test_sre_metrics_fall_back_to_estimates_without_measurements(self) -> None:
        state = AgentState(
            job_id="chaos-estimated",
            repository_url="https://github.com/example/project.git",
            execution_result=SandboxResult(exit_code=0, duration_ms=5000),
        )
        sre = _sre_metrics_from_execution(state)
        self.assertEqual(sre.sli.availability_percent, 100.0)
        self.assertEqual(sre.sli.error_rate, 0.0)
        self.assertEqual(sre.sli.p95_latency_ms, 5000.0)
        self.assertEqual(sre.chaos.scenario, "smoke_validation")
        self.assertEqual(sre.chaos.target, "repository_sandbox")

    def test_sandbox_http_response_preserves_chaos_evidence(self) -> None:
        result = _sandbox_result_from_response(
            '{"exitCode":0,"schemaVersion":"chaos-v1","probeTransport":"kubectl_port_forward",'
            '"baseline":{"metrics":{"availability":1.0}},'
            '"metrics":{"availability":0.75,"recovery_seconds":3.2},'
            '"chaos_observation":{"type":"pod_kill","recovered":true},'
            '"source":{"real_execution_observed":true,"fixture":"fixture-api"}}',
            started_at=0,
        )
        self.assertEqual(result.schema_version, "chaos-v1")
        self.assertEqual(result.probe_transport, "kubectl_port_forward")
        self.assertEqual(result.metrics["recovery_seconds"], 3.2)
        self.assertEqual(result.chaos_observation["type"], "pod_kill")
        self.assertTrue(result.source["real_execution_observed"])

    def test_normalize_github_url_accepts_public_https_repo(self) -> None:
        self.assertEqual(
            _normalize_github_url("https://github.com/CodeReferee-Team/codereferee-AI"),
            "https://github.com/CodeReferee-Team/codereferee-AI.git",
        )
        self.assertIsNone(_normalize_github_url("git@github.com:CodeReferee-Team/codereferee-AI.git"))


if __name__ == "__main__":
    unittest.main()
