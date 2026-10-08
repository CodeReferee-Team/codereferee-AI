"""외부 Prometheus 자원 지표 연결.

cpu/memory는 샌드박스가 null로 보내고(Agent는 remote_write만), 바깥 Prometheus에서
request_id로 장애 구간 값을 뽑아 채운다. 연동 전(설정 없음)이면 아무것도 하지 않고,
어떤 질의 실패도 파이프라인을 막지 않는다.
"""

import io
import json
import unittest
from unittest import mock

from app.config import Settings
from app.metrics import prometheus

WINDOW = {"started_at": "2026-10-02T05:14:45Z", "recovered_at": "2026-10-02T05:15:02Z"}


def _settings(url="http://prom:9090"):
    return Settings(prometheus_url=url)


def _fake_response(result_type, result):
    body = json.dumps({"status": "success", "data": {"resultType": result_type, "result": result}})
    return io.BytesIO(body.encode())


class EnrichGateTests(unittest.TestCase):
    def test_no_url_is_noop(self):
        m = {"cpu_usage_percent": None, "memory_usage_mb": None}
        prometheus.enrich_resource_metrics(m, "req-1", WINDOW, _settings(url=None))
        self.assertIsNone(m["cpu_usage_percent"])

    def test_no_request_id_is_noop(self):
        m = {"cpu_usage_percent": None}
        prometheus.enrich_resource_metrics(m, None, WINDOW, _settings())
        self.assertIsNone(m["cpu_usage_percent"])

    def test_no_chaos_window_is_noop(self):
        m = {"cpu_usage_percent": None}
        prometheus.enrich_resource_metrics(m, "req-1", None, _settings())
        self.assertIsNone(m["cpu_usage_percent"])

    def test_existing_value_is_not_overwritten(self):
        m = {"cpu_usage_percent": 42.0, "memory_usage_mb": 100.0}
        with mock.patch.object(prometheus, "_query_scalar", return_value=999.0):
            prometheus.enrich_resource_metrics(m, "req-1", WINDOW, _settings())
        self.assertEqual(m["cpu_usage_percent"], 42.0)
        self.assertEqual(m["memory_usage_mb"], 100.0)


class EnrichFillTests(unittest.TestCase):
    def test_fills_cpu_and_memory_from_prometheus(self):
        with mock.patch.object(prometheus, "_query_scalar", side_effect=[93.4, 512.0]) as q:
            m = {"cpu_usage_percent": None, "memory_usage_mb": None}
            prometheus.enrich_resource_metrics(m, "req-7", WINDOW, _settings())
        self.assertEqual(m["cpu_usage_percent"], 93.4)
        self.assertEqual(m["memory_usage_mb"], 512.0)
        # request_id와 장애 구간(17초)이 PromQL에 들어가야 한다.
        cpu_promql = q.call_args_list[0].args[0]
        self.assertIn('codereferee_request_id="req-7"', cpu_promql)
        self.assertIn("[17s:1s]", cpu_promql)
        self.assertIn("container_cpu_usage_seconds_total", cpu_promql)

    def test_query_failure_does_not_raise_and_leaves_null(self):
        with mock.patch.object(prometheus, "_query_scalar", side_effect=RuntimeError("prom down")):
            m = {"cpu_usage_percent": None}
            prometheus.enrich_resource_metrics(m, "req-1", WINDOW, _settings())  # 예외 없어야 통과
        self.assertIsNone(m["cpu_usage_percent"])


class QueryScalarTests(unittest.TestCase):
    def test_parses_scalar_result(self):
        with mock.patch("urllib.request.urlopen", return_value=_fake_response("scalar", [1700000000, "88.5"])):
            self.assertEqual(prometheus._query_scalar("x", _settings()), 88.5)

    def test_parses_vector_result(self):
        vec = [{"metric": {}, "value": [1700000000, "256.0"]}]
        with mock.patch("urllib.request.urlopen", return_value=_fake_response("vector", vec)):
            self.assertEqual(prometheus._query_scalar("x", _settings()), 256.0)

    def test_empty_vector_is_none(self):
        with mock.patch("urllib.request.urlopen", return_value=_fake_response("vector", [])):
            self.assertIsNone(prometheus._query_scalar("x", _settings()))

    def test_nan_is_rejected(self):
        with mock.patch("urllib.request.urlopen", return_value=_fake_response("scalar", [1700000000, "NaN"])):
            self.assertIsNone(prometheus._query_scalar("x", _settings()))


if __name__ == "__main__":
    unittest.main()
