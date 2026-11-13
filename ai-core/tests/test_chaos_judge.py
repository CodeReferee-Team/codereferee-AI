"""실제 Litmus 실행 결과로 카오스 판정을 검증한다.

fixture는 sandbox가 2026-10-01~02에 QuickByte_Demo를 Kubernetes에 띄우고 Litmus로 Pod을
삭제해 얻은 실측값이다(codereferee-sandbox의 data/actual). 합성 데이터로 규칙을 맞추면
필드 이름이 어긋나는 것을 이미 겪었기 때문에 실측을 기준으로 삼는다.
"""

import json
import unittest
from pathlib import Path

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport
from app.sandbox.docker_runner import _sandbox_result_from_response
from app.workflow import repository_validation as workflow

FIXTURES = Path(__file__).parent / "fixtures" / "chaos_actual"


def _result(name: str):
    """sandbox 응답을 실제 경로(HTTP 파서)로 통과시킨다."""
    return _sandbox_result_from_response((FIXTURES / f"{name}.json").read_text(encoding="utf-8"), 0.0)


def _state(name: str) -> AgentState:
    state = AgentState(
        job_id="chaos",
        repository_url="https://github.com/phdcoco/QuickByte_Demo.git",
        status=JobStatus.running,
    )
    state.preflight_report = RepositoryPreflightReport(
        repository_url=state.repository_url, cloneable=True, executable=True
    )
    state.execution_result = _result(name)
    state.metrics = workflow._metrics_from_execution(state)
    state.sre_metrics = workflow._sre_metrics_from_execution(state)
    return state


class RealEvidenceShapeTests(unittest.TestCase):
    """계약 문서가 아니라 실제로 오는 값을 확인한다."""

    def test_litmus_payloads_have_no_recovered_boolean(self) -> None:
        # Litmus 경로는 recovered_at만 보낸다. recovered 키를 요구하면 전부 Error가 된다.
        payload = json.loads((FIXTURES / "quickbyte-auto-deploy-litmus-001.json").read_text())
        observation = payload["chaos_observation"]
        self.assertNotIn("recovered", observation)
        self.assertIn("recovered_at", observation)

    def test_payloads_do_not_carry_source_real_execution_observed(self) -> None:
        # 계약 문서에는 있지만 실제 응답에는 없다. 분기 조건이 이것을 요구하면 안 된다.
        payload = json.loads((FIXTURES / "quickbyte-auto-deploy-litmus-001.json").read_text())
        self.assertNotIn("source", payload)

    def test_target_configuration_is_where_the_workload_settings_live(self) -> None:
        payload = json.loads((FIXTURES / "quickbyte-auto-deploy-litmus-001.json").read_text())
        config = payload["chaos_observation"]["target_configuration"]
        self.assertEqual(config["replicas"], 1)
        self.assertEqual(config["termination_grace_period_seconds"], 30)
        self.assertEqual(config["readiness_probe"]["period_seconds"], 5)


class ChaosBranchTests(unittest.TestCase):
    """카오스 실행에는 일반 smoke 규칙을 적용하지 않는다.

    sandbox가 별도 서버 프로세스를 띄우지 않고 Kubernetes Service probe로 관측하므로
    server_started=false와 http_status=null은 실패가 아니라 "그 검사를 하지 않았다"는 뜻이다.
    """

    def test_a_real_chaos_run_is_not_judged_by_smoke_rules(self) -> None:
        state = _state("quickbyte-pod-kill-001")
        report = nodes._fallback_judge(state)
        self.assertNotIn("smoke", str(report["reason"]).lower())
        self.assertNotEqual(report.get("reason_category"), "service_smoke_failed")

    def test_recovered_within_the_bound_passes_with_a_single_replica_warning(self) -> None:
        # 상한은 grace 30 + probe 5 + allowance 10 = 45초(warm-pool 전제). 실측 shape 위에서
        # 복구만 상한 안쪽 값으로 두어 pass+warning 분기를 확인한다. 실측 58.84초는 콜드
        # 클러스터 값이라 warm-pool 상한을 넘는다(아래 over-bound 테스트에 해당).
        state = _state("quickbyte-pod-kill-001")
        state.execution_result.chaos_observation["recovery_seconds"] = 40.0
        state.sre_metrics = workflow._sre_metrics_from_execution(state)
        report = nodes._fallback_judge(state)
        self.assertEqual(report["status"], "Pass")
        self.assertIn("chaos_single_replica_topology", state.metrics["policy_warnings"])

    def test_recovery_over_the_bound_passes_with_a_warning(self) -> None:
        # 같은 설정에서 복구가 125.91초 걸렸다. 상한 45초의 세 배 가까이지만 월간 버짓 안이다.
        # 2026-10-23부터 상한 초과는 경고다(judge-policy 6.2 규칙 4).
        state = _state("quickbyte-auto-deploy-litmus-001")
        report = nodes._fallback_judge(state)
        self.assertEqual(report["status"], "Pass")
        self.assertIn("chaos_recovery_slower_than_expected", state.metrics["policy_warnings"])


class RecoveryDerivationTests(unittest.TestCase):
    """recovered가 없으면 recovered_at으로 판단한다. 없다고 Error로 보내면 안 된다."""

    def test_recovered_at_counts_as_recovered(self) -> None:
        self.assertIsNone(workflow._infra_error_reason(_state("quickbyte-auto-deploy-litmus-001")))

    def test_no_chaos_observation_at_all_is_still_an_error(self) -> None:
        # 관측 전 단계의 산출물이다. 판정 근거가 없다.
        self.assertIsNotNone(workflow._infra_error_reason(_state("quickbyte-litmus-pod-delete-001")))

    def test_abort_condition_triggered_is_an_error(self) -> None:
        state = _state("quickbyte-pod-kill-001")
        state.execution_result.chaos_observation["abort_condition"] = {"triggered": True, "reason": "stop"}
        self.assertEqual(workflow._infra_error_reason(state), "chaos_experiment_aborted")

    def test_untriggered_abort_condition_is_not_an_error(self) -> None:
        # 실측 파일 전부가 {"triggered": false}를 담고 있다. dict가 참이라고 중단으로 보면 안 된다.
        state = _state("quickbyte-pod-kill-001")
        self.assertEqual(state.execution_result.chaos_observation["abort_condition"]["triggered"], False)
        self.assertIsNone(workflow._infra_error_reason(state))


class ScenarioSuiteLatencyTests(unittest.TestCase):
    """여러 시나리오를 한 배포에서 이어 돌린 묶음 응답.

    fixture는 2026-10-19 kind + Litmus에서 받은 실측이다. 묶음은 최상위 metrics의
    p95_latency_ms를 null로 보내고 "시나리오별로 보라"고 적는다. 최상위만 보면 지연 경고가 사라진다.
    """

    def test_suite_reports_latency_only_per_scenario(self) -> None:
        payload = json.loads((FIXTURES / "scenario-20261019-m03-k8s.json").read_text())
        self.assertIsNone(payload["metrics"]["p95_latency_ms"])
        scenario = payload["chaos_observation"]["scenarios"][0]
        self.assertEqual(scenario["chaosMode"], "service_selector_blackhole")
        self.assertGreater(scenario["metrics"]["p95_latency_ms"], 1000)

    def test_latency_degraded_in_one_scenario_warns_for_the_suite(self) -> None:
        # 라우팅 차단에서 p95 1018.93ms 대 그 시나리오 baseline 6.67ms. 단일 시나리오로 돌린
        # S06(1022.68 / 5.4)은 경고가 나온다. 묶음에 넣었다고 같은 관측이 사라지면 안 된다.
        state = _state("scenario-20261019-m03-k8s")
        report = nodes._fallback_judge(state)
        self.assertEqual(report["status"], "Pass")
        self.assertIn("chaos_latency_degraded", state.metrics["policy_warnings"])

    def test_suite_without_degraded_scenario_has_no_latency_warning(self) -> None:
        state = _state("scenario-20261019-m02-proc")
        nodes._fallback_judge(state)
        self.assertNotIn("chaos_latency_degraded", state.metrics["policy_warnings"])


if __name__ == "__main__":
    unittest.main()
