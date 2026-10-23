import unittest

from app.agents import nodes
from app.models import AgentState, JobStatus, SandboxResult


def _state() -> AgentState:
    return AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.running)


def _target_configuration(**overrides) -> dict:
    configuration = {
        "kind": "Deployment",
        "name": "fixture-api",
        "replicas": 3,
        "min_ready_seconds": 0,
        "termination_grace_period_seconds": 30,
        "readiness_probe": {"initial_delay_seconds": 1, "period_seconds": 2, "success_threshold": 1},
    }
    configuration.update(overrides)
    return configuration


def _result(recovery: float, *, observation_extra: dict | None = None) -> SandboxResult:
    observation = {"type": "pod_kill", "recovered": True, "target_configuration": _target_configuration()}
    observation.update(observation_extra or {})
    return SandboxResult(
        exit_code=0,
        metrics={"recovery_seconds": recovery, "availability": 0.99, "error_rate": 0.01, "p95_latency_ms": 30},
        chaos_observation=observation,
        baseline={"metrics": {"p95_latency_ms": 20}},
    )


class ExpectedRecoveryBoundTests(unittest.TestCase):
    """규칙 4. 기대 복구 상한은 워크로드 설정에서 나온다. docs/judge-policy.md 6.5."""

    def test_bound_is_the_sum_of_grace_probe_and_startup_allowance(self) -> None:
        # 30(grace) + 1(initial) + 2*1(probe) + 0(min_ready) + 10(allowance) = 43
        bound = nodes._expected_recovery_bound({"target_configuration": _target_configuration()})
        self.assertEqual(bound, 43.0)

    def test_force_kill_does_not_wait_for_the_grace_period(self) -> None:
        bound = nodes._expected_recovery_bound({"target_configuration": _target_configuration(), "kill_method": "litmus_pod_delete_force"})
        self.assertEqual(bound, 13.0)

    def test_recovery_over_the_bound_is_a_warning_not_a_failure(self) -> None:
        # 2026-10-23 결정. 상한은 우리가 설정에서 계산한 기대값이지 사용자가 겪은 장애가 아니다.
        # 레플리카가 여럿이면 교체가 느려도 남은 Pod가 받고, 하나면 다운타임이 버짓으로 잡힌다.
        # 실서버에서 레플리카를 6까지 올려도 이 규칙에 계속 걸려 치유 루프가 헛돌았다.
        reason, warnings = nodes._measured_policy_findings(_state(), _result(70.0))
        self.assertIsNone(reason)
        self.assertIn("chaos_recovery_slower_than_expected", warnings)

    def test_long_downtime_still_fails_on_the_error_budget(self) -> None:
        # 버짓 판정은 그대로다. 99.9% 목표의 월간 버짓은 2,592초.
        reason, _ = nodes._measured_policy_findings(_state(), _result(3000.0))
        self.assertTrue(reason.startswith("chaos_error_budget_exhausted"))

    def test_the_reason_says_recovery_was_slower_than_the_configuration_implies(self) -> None:
        observation = {"type": "pod_kill", "recovered": True, "recovery_seconds": 70.0,
                       "target_configuration": _target_configuration()}
        sentence = nodes._chaos_reason("recovered within budget", observation,
                                       ["chaos_recovery_slower_than_expected"])
        self.assertIn("70.0s", sentence)
        self.assertIn("43.0s", sentence)

    def test_recovery_within_the_bound_passes(self) -> None:
        reason, warnings = nodes._measured_policy_findings(_state(), _result(12.0))
        self.assertIsNone(reason)
        self.assertNotIn("chaos_single_replica_topology", warnings)

    def test_rule_is_skipped_when_the_target_configuration_is_missing(self) -> None:
        # 근거 없는 상한으로 Fail을 내면 안 된다. 지금 sandbox 응답이 이 상태다.
        self.assertIsNone(nodes._expected_recovery_bound({"type": "pod_kill"}))
        result = _result(999.0)
        result.chaos_observation.pop("target_configuration")
        reason, _ = nodes._measured_policy_findings(_state(), result)
        self.assertFalse((reason or "").startswith("chaos_recovery_exceeds_expected_bound"))

    def test_rule_is_skipped_when_the_probe_config_is_partial(self) -> None:
        configuration = _target_configuration(readiness_probe={"period_seconds": 2})
        self.assertIsNone(nodes._expected_recovery_bound({"target_configuration": configuration}))


class SingleReplicaTopologyTests(unittest.TestCase):
    """규칙 7. replica가 1개면 다운타임은 문서화된 정상 동작이다. Fail이 아니라 경고."""

    def test_single_replica_is_a_warning_not_a_failure(self) -> None:
        result = _result(12.0)
        result.chaos_observation["target_configuration"]["replicas"] = 1
        reason, warnings = nodes._measured_policy_findings(_state(), result)
        self.assertIsNone(reason)
        self.assertIn("chaos_single_replica_topology", warnings)

    def test_single_replica_does_not_mask_a_real_failure(self) -> None:
        result = _result(12.0)
        result.chaos_observation["target_configuration"]["replicas"] = 1
        result.chaos_observation["recovered"] = False
        reason, _ = nodes._measured_policy_findings(_state(), result)
        self.assertTrue(reason.startswith("chaos_not_recovered"))


if __name__ == "__main__":
    unittest.main()
