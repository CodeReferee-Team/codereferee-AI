"""묶음 실행 판정: 일부만 돈 묶음, 묶음 에러 버짓, 경고를 사유에 쓰기.

fixture는 2026-10-19 실측(M09, M10)의 시나리오별 결과를 샌드박스의 새 묶음 집계로 다시 묶은 것이다.
M10은 ck와 cpu가 복구했고 지연 시나리오는 netem이 없어 주입하지 못했다. 예전에는 전체가 ERROR였다.
M09는 레플리카 1개 대상에 장애 두 번, 다운타임 37.7초와 35.5초였다.
"""

import unittest
from pathlib import Path

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport
from app.sandbox.docker_runner import _sandbox_result_from_response
from app.workflow import repository_validation as workflow

FIXTURES = Path(__file__).parent / "fixtures" / "chaos_actual"


def _state(name: str) -> AgentState:
    state = AgentState(job_id="suite", repository_url="https://github.com/test/repo", status=JobStatus.running)
    state.preflight_report = RepositoryPreflightReport(
        repository_url=state.repository_url, cloneable=True, executable=True
    )
    state.execution_result = _sandbox_result_from_response(
        (FIXTURES / f"{name}.json").read_text(encoding="utf-8"), 0.0
    )
    state.metrics = workflow._metrics_from_execution(state)
    state.sre_metrics = workflow._sre_metrics_from_execution(state)
    return state


class PartialSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _state("suite-m10-partial-latency-stopped")

    def test_partial_suite_is_judged_not_an_infra_error(self) -> None:
        self.assertIsNone(workflow._infra_error_reason(self.state))
        state = nodes.judge_node(self.state)
        self.assertEqual(state.judge_report["status"], "Pass")

    def test_partial_suite_names_what_was_not_checked_and_why(self) -> None:
        report = nodes.judge_node(self.state).judge_report
        self.assertIn("chaos_partial_execution", self.state.metrics["policy_warnings"])
        # 지연 검사를 통과한 것으로 읽히면 안 된다.
        self.assertIn("litmus_pod_network_latency", report["reason"])
        self.assertIn("Specified qdisc kind is unknown", report["reason"])
        self.assertIn("2 of 3", report["reason"])
        self.assertTrue(any(item.startswith("not_checked=") for item in report["evidence"]))


class SuiteErrorBudgetTests(unittest.TestCase):
    def test_budget_counts_every_outage_not_only_the_longest(self) -> None:
        state = _state("suite-m09-single-replica-twice")
        observation = state.execution_result.chaos_observation
        self.assertEqual(observation["recovery_seconds"], 37.72)
        self.assertEqual(nodes.chaos_downtime_seconds(observation), 73.2)

    def test_budget_falls_back_to_the_sum_of_scenarios(self) -> None:
        observation = {"recovery_seconds": 10, "scenarios": [
            {"chaos_observation": {"recovery_seconds": 10}}, {"chaos_observation": {"recovery_seconds": 4}}]}
        self.assertEqual(nodes.chaos_downtime_seconds(observation), 14)

    def test_single_experiment_uses_its_recovery(self) -> None:
        self.assertEqual(nodes.chaos_downtime_seconds({"recovery_seconds": 3.5}), 3.5)

    def test_summed_downtime_can_exhaust_the_budget(self) -> None:
        state = _state("suite-m09-single-replica-twice")
        # 복구 상한 규칙(규칙 4)이 먼저 걸리지 않게 readiness 설정을 뺀다. 여기서는 버짓만 본다.
        state.execution_result.chaos_observation["target_configuration"] = {"replicas": 1}
        # 99.99%의 월 버짓은 259.2초. 20%는 51.8초다. 최대값 37.7초로는 경고가 안 붙고 합 73.2초로는 붙는다.
        state.sre_metrics.slo.availability_percent_min = 99.99
        nodes.judge_node(state)
        self.assertIn("chaos_error_budget_significant_burn", state.metrics["policy_warnings"])


class WarningInReasonTests(unittest.TestCase):
    def test_single_replica_downtime_is_in_the_reason(self) -> None:
        # main 기준 M09는 복구 37.7초가 상한 22초를 넘어 Fail이다. 경고 문장은 Fail에도 붙는다.
        report = nodes.judge_node(_state("suite-m09-single-replica-twice")).judge_report
        self.assertIn("1 replica", report["reason"])
        self.assertIn("73.2s", report["reason"])
        self.assertIn("policy_warnings=chaos_single_replica_topology", report["evidence"])

    def test_single_replica_sentence_on_a_pass(self) -> None:
        state = _state("suite-m09-single-replica-twice")
        state.execution_result.chaos_observation["target_configuration"] = {"replicas": 1}
        report = nodes.judge_node(state).judge_report
        self.assertEqual(report["status"], "Pass")
        self.assertTrue(report["reason"].startswith("Chaos experiment recovered within the configured budget."))
        self.assertIn("requests failed for 73.2s", report["reason"])


class StoppedAtTests(unittest.TestCase):
    def test_infra_error_keeps_where_it_stopped(self) -> None:
        body = ('{"schemaVersion": "chaos-v1", "observationStatus": "infrastructure_error", "exitCode": null,'
                ' "infraError": "sandbox_execution_failed", "stoppedAt": "deploy",'
                ' "stderr": "cloning\\nkind load docker-image failed: no space left on device"}')
        state = AgentState(job_id="e", repository_url="https://github.com/test/repo", status=JobStatus.running)
        state.execution_result = _sandbox_result_from_response(body, 0.0)
        metrics = workflow._metrics_from_execution(state)
        self.assertEqual(metrics["stopped_at"], "deploy")
        self.assertEqual(metrics["stopped_reason"], "kind load docker-image failed: no space left on device")


if __name__ == "__main__":
    unittest.main()


class LoadCapacityTests(unittest.TestCase):
    """동시 사용자 부하(2026-10-30). 판정은 바꾸지 않고 몇 명까지 버텼는지를 사유와 경고로 남긴다."""

    def test_capacity_is_reported_without_turning_a_pass_into_a_fail(self) -> None:
        state = _state("suite-load-capacity-local")
        report = nodes.judge_node(state).judge_report
        self.assertIn(nodes.LOAD_ERRORS, state.metrics["policy_warnings"])
        self.assertIn("up to 100 concurrent users; at 200 users 19.4% of requests failed", report["reason"])
        self.assertIn("load.max_users_without_errors=100", report["evidence"])

    def test_clean_load_names_the_highest_stage_and_its_latency(self) -> None:
        load = {"stages": [{"users": 10, "p95_latency_ms": 5.0}, {"users": 200, "p95_latency_ms": 41.2}],
                "max_users_without_errors": 200, "first_users_with_errors": None}
        self.assertEqual(nodes._load_sentence(load),
                         "Under load it answered every request up to 200 concurrent users (p95 41.2ms at 200 users).")

    def test_restart_under_load_is_a_warning(self) -> None:
        state = _state("suite-load-capacity-local")
        observation = state.execution_result.chaos_observation
        observation["scenarios"][0]["chaos_observation"]["restart_count_delta"] = 2
        report = nodes.judge_node(state).judge_report
        self.assertIn(nodes.LOAD_RESTARTED, state.metrics["policy_warnings"])
        self.assertIn("restarted 2 time(s)", report["reason"])

    def test_no_load_scenario_means_no_load_sentence(self) -> None:
        self.assertEqual(nodes.chaos_load(_state("suite-m09-single-replica-twice").execution_result.chaos_observation), {})
