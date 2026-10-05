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
        # replicas=1, 복구 58.84초. 상한은 grace 30 + probe 5 + allowance 30 = 65초.
        state = _state("quickbyte-pod-kill-001")
        report = nodes._fallback_judge(state)
        self.assertEqual(report["status"], "Pass")
        self.assertIn("chaos_single_replica_topology", state.metrics["policy_warnings"])

    def test_recovery_over_the_bound_fails(self) -> None:
        # 같은 설정에서 복구가 125.91초 걸렸다. 상한 65초의 두 배다.
        state = _state("quickbyte-auto-deploy-litmus-001")
        report = nodes._fallback_judge(state)
        self.assertEqual(report["status"], "Fail")
        self.assertEqual(report["reason_category"], "chaos_recovery_exceeds_expected_bound")


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


if __name__ == "__main__":
    unittest.main()
