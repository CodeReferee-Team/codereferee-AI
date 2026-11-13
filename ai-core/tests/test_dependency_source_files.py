"""의존성 장애로 Fail이 나면 그 의존성을 쓰는 코드를 Refiner에게 준다.

2026-10-22 실서버 실측에서 드러났다. 카오스 실패에는 로그에 경로가 없어 Refiner가 받는
파일이 검증 설정(validation.yaml) 하나뿐이었다. replicas나 probe는 고칠 수 있어도,
Redis가 돌아온 뒤 다시 붙지 않는 코드는 보지도 못한다. 의존성 장애에서 고칠 곳은
대개 그 클라이언트를 부르는 모듈이다.
"""

import tempfile
import unittest
from pathlib import Path

from app.agents import source_context
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


def _repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for path, body in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


class DependencyUsersTests(unittest.TestCase):
    def test_the_module_that_imports_the_client_is_attached(self) -> None:
        root = _repo({
            ".codereferee/validation.yaml": "version: 1\n",
            "app.py": "import redis\ncache = redis.Redis()\n",
            "util.py": "import os\n",
        })
        collected = source_context.read_files(
            root, [".codereferee/validation.yaml"], dependency_clients=("redis",)
        )
        self.assertEqual(sorted(collected), [".codereferee/validation.yaml", "app.py"])

    def test_from_imports_and_nested_packages_count(self) -> None:
        root = _repo({"svc/cache.py": "from redis.asyncio import Redis\n"})
        collected = source_context.read_files(root, [], dependency_clients=("redis",))
        self.assertIn("svc/cache.py", collected)

    def test_tests_and_vendored_code_are_not_attached(self) -> None:
        # 테스트 편집은 가드가 막는다. 가상환경 안의 redis 패키지는 사용자 코드가 아니다.
        root = _repo({
            "tests/test_cache.py": "import redis\n",
            ".venv/lib/site.py": "import redis\n",
        })
        collected = source_context.read_files(root, [], dependency_clients=("redis",))
        self.assertEqual(collected, {})

    def test_the_file_cap_still_holds(self) -> None:
        root = _repo({f"m{i}.py": "import redis\n" for i in range(6)})
        collected = source_context.read_files(root, [], dependency_clients=("redis",))
        self.assertEqual(len(collected), source_context.MAX_FILES)

    def test_without_a_dependency_nothing_is_added(self) -> None:
        root = _repo({"app.py": "import redis\n"})
        self.assertEqual(source_context.read_files(root, []), {})


class AttachPassesTheDependencyTests(unittest.TestCase):
    def _clients_for(self, observation: dict) -> tuple:
        from app.workflow import repository_validation as workflow

        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.execution_result = SandboxResult(exit_code=1, chaos_observation=observation)
        state.judge_report = {"status": "Fail", "reason_category": "chaos_not_recovered"}
        captured: dict = {}

        def _collect(url, paths, **kwargs):
            captured.update(kwargs)
            return {}

        original = source_context.collect
        source_context.collect = _collect
        try:
            workflow.attach_source_files(state)
        finally:
            source_context.collect = original
        return tuple(captured.get("dependency_clients") or ())

    def test_redis_outage(self) -> None:
        self.assertIn("redis", self._clients_for({"type": "dependency_redis_outage", "recovered": False}))

    def test_database_outage(self) -> None:
        clients = self._clients_for({"type": "dependency_database_outage", "recovered": False})
        self.assertIn("psycopg", clients)
        self.assertIn("sqlalchemy", clients)

    def test_a_batch_names_the_dependency_inside_a_scenario(self) -> None:
        observation = {
            "type": "scenario_suite",
            "scenarios": [
                {"chaosMode": "litmus_container_kill", "chaos_observation": {"type": "container_kill"}},
                {"chaosMode": "dependency_redis_outage",
                 "chaos_observation": {"type": "dependency_redis_outage", "recovered": False}},
            ],
        }
        self.assertIn("redis", self._clients_for(observation))

    def test_a_pod_fault_adds_no_client(self) -> None:
        self.assertEqual(self._clients_for({"type": "container_kill", "recovered": False}), ())


class RefinerSeesWhichFaultTests(unittest.TestCase):
    def test_the_packet_names_the_fault(self) -> None:
        # replicas를 올려도 모든 Pod가 같은 결함을 갖고 있으면 복구되지 않는다. 모델이 어떤
        # 장애였는지 알아야 설정 대신 클라이언트 코드를 고친다.
        from app.agents.evidence import build_llm_evidence_packet, build_refiner_evidence

        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.execution_result = SandboxResult(
            exit_code=1,
            chaos_observation={"type": "dependency_redis_outage", "recovered": False,
                               "target_configuration": {"replicas": 1}},
        )
        state.judge_report = {"status": "Fail", "reason_category": "chaos_not_recovered"}
        self.assertEqual(build_refiner_evidence(state)["chaos"]["fault"], "dependency_redis_outage")
        self.assertEqual(build_llm_evidence_packet(state)["chaos"]["fault"], "dependency_redis_outage")


if __name__ == "__main__":
    unittest.main()
