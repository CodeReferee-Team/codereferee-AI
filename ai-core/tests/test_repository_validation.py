import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.nodes import critic_node, judge_node, planner_node, refiner_node
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.repository.preflight import _normalize_github_url
from app.storage.sqlite_store import SQLiteJobStore, SQLitePatchStore
from app.agents.evidence import build_evidence_packet, summarize_metrics
from app.sandbox.docker_runner import _extract_sandbox_report, _sandbox_result_from_response
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
                self.published = []
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

            def publish(self, event):
                self.published.append(event)
                return len(self.published)

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
            published: list = []

            def dequeue(self, *, block=False, timeout=0):
                return {
                    "taskId": "job-pass",
                    "repositoryUrl": "https://github.com/example/project",
                    "branch": "main",
                    "commitSha": None,
                    "submittedAt": "2026-05-26T10:00:00",
                }

            def publish(self, event):
                self.published.append(event)
                return len(self.published)

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

    class FakeOutputQueue:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def publish(self, event: dict) -> int:
            self.events.append(event)
            return len(self.events)

    def _run_with_output(self, preflight, sandbox_result, *, request_id="req-1"):
        state = AgentState(
            job_id="job-1", request_id=request_id, repository_url="https://github.com/example/project.git"
        )
        output = self.FakeOutputQueue()
        with patch(
            "app.workflow.repository_validation.repository_preflight_runner.run", return_value=preflight
        ), patch(
            "app.workflow.repository_validation.sandbox_runner.run_repository", return_value=sandbox_result
        ), patch("app.workflow.repository_validation.record_validation_artifacts"):
            state = execute_repository_validation(state, output_queue=output)
        return state, output.events

    def test_emits_progress_steps_then_single_result(self) -> None:
        _, events = self._run_with_output(
            self._reachable_preflight(), SandboxResult(exit_code=0, stdout="ok", duration_ms=100)
        )
        steps = [e["step"] for e in events if e["type"] == "progress"]
        results = [e for e in events if e["type"] == "result"]
        self.assertEqual(steps, ["PREFLIGHT", "BASELINE", "JUDGING"])
        self.assertEqual(len(results), 1)
        self.assertIs(events[-1], results[0])  # result 뒤에는 어떤 이벤트도 오지 않는다

    def test_result_event_matches_server_contract(self) -> None:
        _, events = self._run_with_output(
            self._reachable_preflight(), SandboxResult(exit_code=0, stdout="ok", duration_ms=100)
        )
        result = events[-1]
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["status"], "success")
        for key in (
            "repository_url", "branch", "commit_sha", "validation_plan", "preflight_report",
            "execution_result", "judge_report", "critic_feedback", "refiner_report", "metrics", "events",
        ):
            self.assertIn(key, result)

    def test_user_code_failure_reports_status_fail(self) -> None:
        _, events = self._run_with_output(
            self._reachable_preflight(), SandboxResult(exit_code=1, stderr="pytest failed")
        )
        self.assertEqual(events[-1]["status"], "fail")
        self.assertTrue(events[-1]["critic_feedback"])

    def test_infra_error_reports_status_error_without_judging_progress(self) -> None:
        _, events = self._run_with_output(
            self._reachable_preflight(),
            SandboxResult(exit_code=None, stderr="docker down", infra_error="docker_daemon_unreachable"),
        )
        self.assertEqual(events[-1]["status"], "error")
        self.assertNotIn("JUDGING", [e["step"] for e in events if e["type"] == "progress"])

    def test_chaos_progress_is_reported_only_when_chaos_ran(self) -> None:
        chaos_result = SandboxResult(
            exit_code=0,
            duration_ms=5000,
            metrics={"availability": 0.99, "recovery_seconds": 2.0},
            baseline={"metrics": {"p95_latency_ms": 12.0}},
            chaos_observation={"type": "pod_kill", "recovered": True},
        )
        _, events = self._run_with_output(self._reachable_preflight(), chaos_result)
        self.assertEqual(
            [e["step"] for e in events if e["type"] == "progress"],
            ["PREFLIGHT", "BASELINE", "CHAOS", "JUDGING"],
        )

    def test_no_events_without_request_id(self) -> None:
        _, events = self._run_with_output(
            self._reachable_preflight(), SandboxResult(exit_code=0, duration_ms=10), request_id=None
        )
        self.assertEqual(events, [])

    def test_job_survives_across_store_instances(self) -> None:
        # API와 worker는 별도 프로세스다. 한쪽이 저장한 job을 다른 쪽이 읽을 수 있어야 한다.
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "codereferee.sqlite3"
            writer = SQLiteJobStore(db)
            writer.save(
                AgentState(
                    job_id="cross-process",
                    request_id="req-1",
                    repository_url="https://github.com/example/project.git",
                    status=JobStatus.success,
                    events=["Workflow: repository validation started"],
                )
            )
            reader = SQLiteJobStore(db)
            loaded = reader.get("cross-process")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.status, JobStatus.success)
            self.assertEqual(loaded.request_id, "req-1")
            self.assertEqual(loaded.events, ["Workflow: repository validation started"])

    def test_job_store_returns_none_for_unknown_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(SQLiteJobStore(Path(tmpdir) / "db.sqlite3").get("missing"))

    def test_job_store_overwrites_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteJobStore(Path(tmpdir) / "db.sqlite3")
            state = AgentState(job_id="j", repository_url="https://github.com/example/p.git")
            store.save(state)
            state.status = JobStatus.error
            store.save(state)
            loaded = store.get("j")
            assert loaded is not None
            self.assertEqual(loaded.status, JobStatus.error)

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


class SandboxResultContractTests(unittest.TestCase):
    """샌드박스 결과 JSON 계약: Judge는 로그 전문이 아니라 이 구조화 결과로 판정한다."""

    def _failed_result(self) -> SandboxResult:
        logs = (
            "[CodeReferee] preparing sandbox\n"
            "[CodeReferee] cloning repository\n"
            "fatal: Remote branch no-such-branch not found\n"
            '[CodeReferee:RESULT] {"schema_version":"sandbox-result.v1","detected_stack":"unknown",'
            '"outcome":"failure","failed_step":"clone","exit_code":128,'
            '"steps":[{"name":"prepare","exit_code":0,"duration_ms":1},'
            '{"name":"clone","exit_code":128,"duration_ms":524}]}'
        )
        report, clean = _extract_sandbox_report(logs)
        return SandboxResult(exit_code=128, stderr=clean, sandbox_report=report)

    def test_extract_sandbox_report_removes_sentinel_line_from_logs(self) -> None:
        result = self._failed_result()
        self.assertEqual(result.sandbox_report["failed_step"], "clone")
        self.assertEqual(result.sandbox_report["steps"][1]["exit_code"], 128)
        # 센티널 줄은 로그 본문에 남지 않아야 중복 노출이 생기지 않는다.
        self.assertNotIn("CodeReferee:RESULT", result.stderr)
        self.assertIn("Remote branch no-such-branch not found", result.stderr)

    def test_malformed_sentinel_is_kept_as_plain_log(self) -> None:
        report, clean = _extract_sandbox_report("[CodeReferee:RESULT] not-json\nother line")
        self.assertEqual(report, {})
        self.assertIn("not-json", clean)

    def test_sandbox_summary_is_one_line(self) -> None:
        summary = self._failed_result().sandbox_summary
        self.assertIn("failed_step=clone", summary)
        self.assertIn("clone:128(524ms)", summary)
        self.assertNotIn("\n", summary)

    def test_judge_reason_is_a_sentence_not_a_log_dump(self) -> None:
        state = AgentState(
            job_id="t",
            repository_url="https://github.com/example/project.git",
            preflight_report=RepositoryPreflightReport(
                repository_url="https://github.com/example/project.git", cloneable=True, executable=True
            ),
            execution_result=self._failed_result(),
        )
        state = judge_node(state)
        reason = state.judge_report["reason"]
        self.assertEqual(state.status, JobStatus.failed)
        self.assertIn("clone", reason)
        self.assertLess(len(reason), 200)
        # 스택을 모를 때 "unknown"을 문장에 넣으면 진단이 모호해진다.
        self.assertNotIn("unknown", reason.casefold())
        # 로그 전문이 evidence에 통째로 들어가면 Critic 프롬프트가 중복으로 비대해진다.
        self.assertTrue(all(len(item) <= 500 for item in state.judge_report["evidence"]))

    def test_evidence_packet_trims_unbounded_chaos_observation(self) -> None:
        metrics = {
            "exit_code": 0,
            "chaos_observation": {
                "type": "pod_kill",
                "kubernetes_events": [{"reason": "Scheduled", "message": "m" * 100} for _ in range(60)],
                "replacement_logs": "line\n" * 500,
            },
        }
        trimmed = summarize_metrics(metrics)["chaos_observation"]
        self.assertEqual(trimmed["kubernetes_events_total"], 60)
        self.assertEqual(len(trimmed["kubernetes_events"]), 20)
        self.assertLess(len(trimmed["replacement_logs"]), 700)
        # 원본은 건드리지 않는다. Backend로는 전문이 그대로 가야 한다.
        self.assertEqual(len(metrics["chaos_observation"]["kubernetes_events"]), 60)


if __name__ == "__main__":
    unittest.main()
