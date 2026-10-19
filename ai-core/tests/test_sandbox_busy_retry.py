"""샌드박스가 다른 요청으로 바쁠 때(409) 바로 ERROR로 끝내지 않고 기다렸다 다시 보낸다.

샌드박스는 실험 락을 잡지 못하면 아무 작업도 시작하지 않고 409를 돌려준다
(codereferee-sandbox app/main.py). 그래서 같은 요청을 다시 보내도 중복 실행이 없다.
지금까지는 409가 sandbox_http_error로 끝나 앞 요청이 끝나기만 기다리면 됐을 검증이 ERROR가 됐다.
"""
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.sandbox.docker_runner import SandboxRunner


def http_error(code: int, body: str = "") -> HTTPError:
    return HTTPError("http://sandbox/repositories/validate", code, "err", {}, io.BytesIO(body.encode()))


def ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    return response


class SandboxBusyRetryTests(unittest.TestCase):
    def setUp(self):
        self.runner = SandboxRunner()
        self.runner.settings = self.runner.settings.model_copy(update={
            "sandbox_base_url": "http://sandbox",
            "sandbox_busy_max_wait_seconds": 60,
            "sandbox_busy_retry_interval_seconds": 20,
        })

    @patch("app.sandbox.docker_runner.time.sleep")
    @patch("app.sandbox.docker_runner.urlopen")
    def test_busy_sandbox_is_retried_until_it_accepts(self, urlopen, sleep):
        urlopen.side_effect = [http_error(409, '{"detail":"A Chaos v1 experiment is already running."}'),
                               ok_response({"exitCode": 0, "observationStatus": "observed"})]
        result = self.runner.run_repository("https://github.com/test/repo", chaos_mode="suite_quick")
        self.assertIsNone(result.infra_error)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(20)

    @patch("app.sandbox.docker_runner.time.sleep")
    @patch("app.sandbox.docker_runner.urlopen")
    def test_sandbox_still_busy_after_the_wait_budget_is_its_own_infra_error(self, urlopen, sleep):
        urlopen.side_effect = [http_error(409) for _ in range(10)]
        result = self.runner.run_repository("https://github.com/test/repo", chaos_mode="suite_quick")
        self.assertEqual(result.infra_error, "sandbox_busy")
        self.assertIsNone(result.exit_code)
        self.assertIn("60s", result.stderr)
        self.assertEqual(urlopen.call_count, 4)  # 처음 1번 + 20초씩 3번 기다린 뒤 재시도
        self.assertEqual(sleep.call_count, 3)

    @patch("app.sandbox.docker_runner.time.sleep")
    @patch("app.sandbox.docker_runner.urlopen")
    def test_other_http_errors_are_not_retried(self, urlopen, sleep):
        urlopen.side_effect = [http_error(500, "boom")]
        result = self.runner.run_repository("https://github.com/test/repo", chaos_mode="suite_quick")
        self.assertEqual(result.infra_error, "sandbox_http_error")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
