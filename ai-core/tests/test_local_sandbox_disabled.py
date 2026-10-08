"""layer-1 기능을 layer-2로 옮긴 뒤, ai-core 로컬 Docker 샌박은 기본 비활성화다.

SANDBOX_BASE_URL이 없고 local_sandbox_enabled=false면 조용히 로컬로 떨어지지 않고
명확히 막아야 한다. 설정을 올리면 기존 로컬 경로가 그대로 쓰인다.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.sandbox.docker_runner import SandboxRunner


def _runner(*, base_url=None, local_enabled=False) -> SandboxRunner:
    runner = SandboxRunner()
    runner.settings = SimpleNamespace(sandbox_base_url=base_url, local_sandbox_enabled=local_enabled)
    return runner


class LocalSandboxDisabledTests(unittest.TestCase):
    def test_no_base_url_and_disabled_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _runner(base_url=None, local_enabled=False).run_repository("https://github.com/o/r")
        self.assertIn("local_sandbox_enabled", str(ctx.exception))

    def test_base_url_routes_to_http(self) -> None:
        runner = _runner(base_url="http://localhost:8100", local_enabled=False)
        with patch.object(runner, "_run_repository_via_http", return_value="http") as http:
            self.assertEqual(runner.run_repository("https://github.com/o/r"), "http")
        http.assert_called_once()

    def test_enabled_without_base_url_uses_local(self) -> None:
        runner = _runner(base_url=None, local_enabled=True)
        with patch.object(runner, "_run_repository_via_local_docker", return_value="local") as local:
            self.assertEqual(runner.run_repository("https://github.com/o/r"), "local")
        local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
