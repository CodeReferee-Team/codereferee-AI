import unittest

from app.agents.evidence import classify_failure_category, _primary_signal
from app.agents.nodes import _refiner_guidance
from app.models import AgentState, RepositoryPreflightReport, SandboxResult


def _reachable_state(**exec_kwargs) -> AgentState:
    return AgentState(
        job_id="t",
        repository_url="https://github.com/o/r",
        preflight_report=RepositoryPreflightReport(
            repository_url="https://github.com/o/r",
            cloneable=True,
            executable=True,
            reason="reachable",
        ),
        execution_result=SandboxResult(**exec_kwargs),
    )


class FailureClassificationTests(unittest.TestCase):
    """배포 롤아웃 타임아웃은 일반 non_zero_exit과 구분돼야 한다.

    근거: http500(probe 500)·crashloop(부팅 크래시)가 둘 다 'run 단계 kubectl rollout
    타임아웃'으로 들어오는데, 샌박이 pod 진단을 안 실어 근본원인은 구분 불가다. 최소한
    '테스트/빌드가 깨진 non_zero_exit'과 '배포가 준비 상태에 도달 못한 rollout 타임아웃'은
    단계 수준에서 갈라야 Critic이 정직하게 진단한다.
    """

    def test_rollout_timeout_is_its_own_category(self) -> None:
        state = _reachable_state(
            exit_code=1,
            timed_out=False,
            stderr="kubectl --context kind-codereferee: error: timed out waiting for the condition",
            sandbox_report={"failed_step": "run"},
        )
        self.assertEqual(classify_failure_category(state), "deploy_rollout_timeout")
        self.assertIn("rollout", _primary_signal(state, "deploy_rollout_timeout").lower())

    def test_genuine_test_failure_stays_non_zero_exit(self) -> None:
        state = _reachable_state(
            exit_code=1,
            timed_out=False,
            stderr="AssertionError: assert add(2, 3) == 5",
            sandbox_report={"failed_step": "test"},
        )
        self.assertEqual(classify_failure_category(state), "non_zero_exit")

    def test_rollout_marker_without_run_step_not_reclassified(self) -> None:
        # 타임아웃 문자열이 run이 아닌 단계에서 나오면 재분류하지 않는다(정밀 게이트).
        state = _reachable_state(
            exit_code=1,
            timed_out=False,
            stderr="timed out waiting for the condition",
            sandbox_report={"failed_step": "install"},
        )
        self.assertEqual(classify_failure_category(state), "non_zero_exit")


_ROLLOUT_STDERR = "kubectl: error: timed out waiting for the condition"


def _rollout_state(pod_diagnostics: dict) -> AgentState:
    # #111 pod 진단이 실린 rollout 타임아웃 상태.
    return _reachable_state(
        exit_code=1,
        timed_out=False,
        stderr=_ROLLOUT_STDERR,
        sandbox_report={"failed_step": "run", "pod_diagnostics": pod_diagnostics},
    )


class RolloutSubtypeTests(unittest.TestCase):
    """pod 진단(#111)이 실리면 deploy_rollout_timeout을 근본원인별로 세분한다.

    진단이 없으면(샌박 미구현) 기존처럼 deploy_rollout_timeout으로 정직하게 남긴다.
    """

    def test_crashloop_subtype_surfaces_log(self) -> None:
        diag = {"pods": [{
            "name": "app-1", "phase": "Running",
            "logs_tail": "Traceback (most recent call last):\nKeyError: 'DATABASE_URL'",
            "containers": [{"name": "app", "ready": False, "restart_count": 5,
                            "waiting_reason": "CrashLoopBackOff",
                            "last_terminated": {"reason": "Error", "exit_code": 1}}],
            "events": [{"reason": "BackOff", "message": "Back-off restarting failed container", "count": 5}],
        }]}
        state = _rollout_state(diag)
        self.assertEqual(classify_failure_category(state), "deploy_crashloop")
        sig = _primary_signal(state, "deploy_crashloop")
        self.assertIn("CrashLoopBackOff", sig)
        self.assertIn("DATABASE_URL", sig)  # 로그 꼬리가 근본원인을 드러냄

    def test_probe_failure_subtype(self) -> None:
        diag = {"pods": [{
            "name": "app-1", "phase": "Running", "logs_tail": "",
            "containers": [{"name": "app", "ready": False, "restart_count": 0,
                            "waiting_reason": None, "last_terminated": None}],
            "events": [{"reason": "Unhealthy", "message": "Readiness probe failed: HTTP 500", "count": 9}],
        }]}
        state = _rollout_state(diag)
        self.assertEqual(classify_failure_category(state), "deploy_probe_failure")
        self.assertIn("readiness", _primary_signal(state, "deploy_probe_failure").lower())

    def test_image_pull_subtype(self) -> None:
        diag = {"pods": [{
            "name": "app-1", "phase": "Pending", "logs_tail": "",
            "containers": [{"name": "app", "ready": False,
                            "waiting_reason": "ImagePullBackOff", "last_terminated": None}],
            "events": [],
        }]}
        state = _rollout_state(diag)
        self.assertEqual(classify_failure_category(state), "deploy_image_pull_error")

    def test_rollout_without_diagnostics_stays_generic(self) -> None:
        self.assertEqual(classify_failure_category(_rollout_state({})), "deploy_rollout_timeout")


class RefinerRoutingTests(unittest.TestCase):
    def test_crashloop_routes_to_env_guard(self) -> None:
        guidance = _refiner_guidance(
            "deploy crashloop",
            "missing env DATABASE_URL caused a boot crash (CrashLoopBackOff)",
            "patch the boot path",
        )
        self.assertTrue(any("environment" in g.lower() or "env" in g.lower() or "guard" in g.lower()
                            for g in guidance))

    def test_probe_failure_routes_to_health_path(self) -> None:
        guidance = _refiner_guidance(
            "deploy probe failure",
            "readiness probe Unhealthy: health path returns 500",
            "fix the health endpoint",
        )
        self.assertTrue(any("readiness" in g.lower() or "health" in g.lower() for g in guidance))


if __name__ == "__main__":
    unittest.main()
