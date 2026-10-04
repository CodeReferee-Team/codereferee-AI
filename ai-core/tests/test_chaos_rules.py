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
        # 30(grace) + 1(initial) + 2*1(probe) + 0(min_ready) + 30(allowance) = 63
        bound = nodes._expected_recovery_bound({"target_configuration": _target_configuration()})
        self.assertEqual(bound, 63.0)

    def test_force_kill_does_not_wait_for_the_grace_period(self) -> None:
        bound = nodes._expected_recovery_bound({"target_configuration": _target_configuration(), "kill_method": "litmus_pod_delete_force"})
        self.assertEqual(bound, 33.0)

    def test_recovery_over_the_bound_fails(self) -> None:
        reason, _ = nodes._measured_policy_findings(_state(), _result(70.0))
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("chaos_recovery_exceeds_expected_bound"))

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
