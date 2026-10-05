"""자원 지표 SLO 위반을 판정한다.

sandbox가 CPU·메모리·재시작·연결 오류·요청 수를 보내고 데이터셋이 그 임계값을 담고 있는데,
SLO 모델에 담을 자리가 없어 pydantic이 조용히 버렸다. 그래서 판정이 비교할 값을 못 받고
CPU 96%짜리 레포가 all_checks_passed로 나갔다. 사유 코드 7종이 도달 불가 상태였다.
"""

import unittest

from app.agents import nodes
from app.models import SLO, AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


def _state(metrics: dict, slo: dict) -> AgentState:
    state = AgentState(job_id="m", repository_url="https://github.com/o/r", status=JobStatus.running)
    state.preflight_report = RepositoryPreflightReport(
        repository_url="https://github.com/o/r", cloneable=True, executable=True
    )
    state.execution_result = SandboxResult(exit_code=0, duration_ms=1000, metrics=metrics)
    from app.workflow import repository_validation as workflow

    state.metrics = workflow._metrics_from_execution(state)
    state.sre_metrics = workflow._sre_metrics_from_execution(state)
    state.sre_metrics.slo = SLO(**slo)
    return state


BASE_SLO = {"p95_latency_ms_max": 300, "error_rate_max": 0.01}
CLEAN = {"p95_latency_ms": 200, "error_rate": 0.0, "availability": 1.0}


class SLOFieldTests(unittest.TestCase):
    def test_the_slo_model_keeps_the_resource_thresholds(self) -> None:
        # pydantic이 모르는 필드를 버리기 때문에, 자리가 없으면 임계값이 사라진다.
        slo = SLO(
            cpu_usage_percent_max=80,
            memory_usage_ratio_max=0.8,
            restart_count_max=0,
            db_connection_errors_max=0,
            redis_connection_errors_max=0,
            request_count_min=1,
        )
        self.assertEqual(slo.cpu_usage_percent_max, 80)
        self.assertEqual(slo.memory_usage_ratio_max, 0.8)
        self.assertEqual(slo.restart_count_max, 0)
        self.assertEqual(slo.db_connection_errors_max, 0)
        self.assertEqual(slo.redis_connection_errors_max, 0)
        self.assertEqual(slo.request_count_min, 1)

    def test_the_defaults_do_not_invent_resource_thresholds(self) -> None:
        # 근거 없는 상한으로 Fail을 내면 안 된다. 운영자가 정해야 발동한다.
        from app.models import DEFAULT_SLO

        self.assertIsNone(DEFAULT_SLO.cpu_usage_percent_max)
        self.assertIsNone(DEFAULT_SLO.restart_count_max)


class ResourceViolationTests(unittest.TestCase):
    def _judge(self, metrics: dict, slo: dict) -> dict:
        return nodes._fallback_judge(_state({**CLEAN, **metrics}, {**BASE_SLO, **slo}))

    def test_cpu_over_the_target_fails(self) -> None:
        report = self._judge({"cpu_usage_percent": 96}, {"cpu_usage_percent_max": 80})
        self.assertEqual(report["status"], "Fail")
        self.assertEqual(report["reason_category"], "cpu_saturation")

    def test_memory_ratio_over_the_target_fails(self) -> None:
        report = self._judge(
            {"memory_usage_mb": 980, "memory_limit_mb": 1024}, {"memory_usage_ratio_max": 0.8}
        )
        self.assertEqual(report["reason_category"], "memory_pressure")

    def test_a_restart_over_the_target_fails(self) -> None:
        report = self._judge({"restart_count": 1}, {"restart_count_max": 0})
        self.assertEqual(report["reason_category"], "unexpected_restart")

    def test_database_connection_errors_fail(self) -> None:
        report = self._judge({"db_connection_errors": 5}, {"db_connection_errors_max": 0})
        self.assertEqual(report["reason_category"], "database_connection_errors")

    def test_redis_connection_errors_fail(self) -> None:
        report = self._judge({"redis_connection_errors": 3}, {"redis_connection_errors_max": 0})
        self.assertEqual(report["reason_category"], "redis_connection_errors")

    def test_no_traffic_fails_before_the_other_metrics_are_trusted(self) -> None:
        # 요청이 0건이면 가용성 100%와 오류율 0%는 아무것도 뜻하지 않는다.
        report = self._judge({"request_count": 0}, {"request_count_min": 1})
        self.assertEqual(report["reason_category"], "no_traffic_observed")

    def test_all_null_measurements_are_not_a_pass(self) -> None:
        state = _state(
            {"p95_latency_ms": None, "error_rate": None, "availability": None, "restart_count": 0},
            BASE_SLO,
        )
        report = nodes._fallback_judge(state)
        self.assertEqual(report["status"], "Fail")
        self.assertEqual(report["reason_category"], "missing_metrics")

    def test_values_within_the_targets_still_pass(self) -> None:
        report = self._judge(
            {"cpu_usage_percent": 50, "memory_usage_mb": 300, "memory_limit_mb": 1024,
             "restart_count": 0, "db_connection_errors": 0, "request_count": 20},
            {"cpu_usage_percent_max": 80, "memory_usage_ratio_max": 0.8, "restart_count_max": 0,
             "db_connection_errors_max": 0, "request_count_min": 1},
        )
        self.assertEqual(report["status"], "Pass")
        self.assertEqual(report["reason_category"], "all_checks_passed")

    def test_a_threshold_the_operator_did_not_set_never_fails(self) -> None:
        report = self._judge({"cpu_usage_percent": 99, "restart_count": 7}, {})
        self.assertEqual(report["status"], "Pass")


if __name__ == "__main__":
    unittest.main()
