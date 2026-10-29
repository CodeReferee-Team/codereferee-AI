"""샌드박스 응답에 실려 오는 1초 해상도 자원 시계열로 cpu/memory를 채운다.

샌드박스는 `source.metrics_observation`에 컨테이너별 `cpu_cores`와
`memory_working_set_bytes`를 range 질의 결과 그대로 싣는다. 2026-10-29 실서버에서
이 값이 있는데도 `cpu_usage_percent`·`memory_usage_mb`가 null로 나갔다. AI는 바깥
Prometheus만 보고 있었고, 그 경로는 recovered_at이 없으면(복구 실패) 구간을 못 잡는다.

픽스처는 그날 실서버 응답에서 발췌했다(tests/fixtures/chaos_actual/server-20261029-*).
"""

import json
import unittest
from pathlib import Path

from app.metrics import observation

FIXTURES = Path(__file__).parent / "fixtures" / "chaos_actual"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _series(pod, points):
    return {"metric": {"pod": pod}, "values": [[t, str(v)] for t, v in points]}


# 2026-10-29T14:00:00Z
T0 = 1793282400.0


def _iso(offset):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(T0 + offset, timezone.utc).isoformat()


class RealResponseTests(unittest.TestCase):
    def test_suite_peaks_sum_pods_inside_scenario_windows(self):
        case = _load("server-20261029-e01-suite-metrics.json")
        m = {"cpu_usage_percent": None, "memory_usage_mb": None}
        observation.fill_resource_metrics(m, case["metrics_observation"], case["chaos_observation"])
        self.assertAlmostEqual(m["cpu_usage_percent"], 10.8957, places=3)
        # 구간 밖(첫 시나리오 이전) 최대 35.60MB는 들어가지 않는다.
        self.assertAlmostEqual(m["memory_usage_mb"], 35.0945, places=3)
        self.assertEqual(m["memory_limit_mb"], 536.870912)

    def test_unrecovered_window_runs_to_end_of_observation(self):
        case = _load("server-20261029-e02-redis-metrics.json")
        self.assertIsNone(case["chaos_observation"]["recovered_at"])
        m = {"cpu_usage_percent": None, "memory_usage_mb": None}
        observation.fill_resource_metrics(m, case["metrics_observation"], case["chaos_observation"])
        self.assertAlmostEqual(m["cpu_usage_percent"], 0.8125, places=3)
        self.assertAlmostEqual(m["memory_usage_mb"], 40.8494, places=3)


class WindowTests(unittest.TestCase):
    def _obs(self, cpu, mem=None):
        return {"started_at": T0, "ended_at": T0 + 100, "cpu_cores": cpu, "memory_working_set_bytes": mem or []}

    def test_points_outside_fault_window_are_ignored(self):
        # 기동 직후 JVM 같은 CPU 스파이크가 장애 구간 최대치로 섞이면 안 된다.
        cpu = [_series("a", [(T0 + 1, 0.95), (T0 + 30, 0.20), (T0 + 40, 0.30)])]
        chaos = {"started_at": _iso(20), "recovered_at": _iso(50)}
        m = {"cpu_usage_percent": None}
        observation.fill_resource_metrics(m, self._obs(cpu), chaos)
        self.assertAlmostEqual(m["cpu_usage_percent"], 30.0)

    def test_pods_at_same_instant_are_summed(self):
        cpu = [_series("a", [(T0 + 30, 0.2)]), _series("b", [(T0 + 30, 0.3)])]
        chaos = {"started_at": _iso(20), "recovered_at": _iso(50)}
        m = {"cpu_usage_percent": None}
        observation.fill_resource_metrics(m, self._obs(cpu), chaos)
        self.assertAlmostEqual(m["cpu_usage_percent"], 50.0)

    def test_no_fault_window_leaves_null(self):
        # 장애 구간을 모르면 어느 값을 볼지 정할 근거가 없다. 전체 최대로 메우지 않는다.
        cpu = [_series("a", [(T0 + 30, 0.2)])]
        m = {"cpu_usage_percent": None}
        observation.fill_resource_metrics(m, self._obs(cpu), {})
        self.assertIsNone(m["cpu_usage_percent"])


class GateTests(unittest.TestCase):
    def test_missing_observation_is_noop(self):
        m = {"cpu_usage_percent": None}
        observation.fill_resource_metrics(m, None, {"started_at": _iso(0)})
        self.assertIsNone(m["cpu_usage_percent"])

    def test_existing_value_is_not_overwritten(self):
        case = _load("server-20261029-e02-redis-metrics.json")
        m = {"cpu_usage_percent": 42.0, "memory_usage_mb": 100.0}
        observation.fill_resource_metrics(m, case["metrics_observation"], case["chaos_observation"])
        self.assertEqual(m["cpu_usage_percent"], 42.0)
        self.assertEqual(m["memory_usage_mb"], 100.0)

    def test_malformed_values_do_not_raise(self):
        obs = {"started_at": T0, "ended_at": T0 + 100,
               "cpu_cores": [{"values": [["x", "NaN"], [T0 + 30]]}, "junk"],
               "memory_working_set_bytes": "junk"}
        m = {"cpu_usage_percent": None, "memory_usage_mb": None}
        observation.fill_resource_metrics(m, obs, {"started_at": _iso(20), "recovered_at": _iso(50)})
        self.assertIsNone(m["cpu_usage_percent"])
        self.assertIsNone(m["memory_usage_mb"])

    def test_mixed_memory_limits_are_not_reported(self):
        # 파드마다 한도가 다르면 하나의 한도로 비율을 낼 수 없다.
        obs = {"pod_bindings": [{"resources": {"limits": {"memory": "512Mi"}}},
                                {"resources": {"limits": {"memory": "1Gi"}}}]}
        m = {}
        observation.fill_resource_metrics(m, obs, {})
        self.assertNotIn("memory_limit_mb", m)


class WorkflowWiringTests(unittest.TestCase):
    def test_sandbox_result_gets_resource_metrics_without_prometheus(self):
        from unittest import mock
        from app.config import Settings
        from app.models import SandboxResult
        from app.workflow import repository_validation as rv

        case = _load("server-20261029-e02-redis-metrics.json")
        result = SandboxResult(
            exit_code=1, stdout="", stderr="", timed_out=True, duration_ms=1,
            metrics={"cpu_usage_percent": None, "memory_usage_mb": None},
            chaos_observation=case["chaos_observation"],
            source={"metrics_observation": case["metrics_observation"]},
        )
        with mock.patch.object(rv, "get_settings", return_value=Settings(prometheus_url=None)):
            rv._fill_resource_metrics(result, "req-1")
        self.assertAlmostEqual(result.metrics["cpu_usage_percent"], 0.8125, places=3)
        self.assertAlmostEqual(result.metrics["memory_usage_mb"], 40.8494, places=3)


if __name__ == "__main__":
    unittest.main()
