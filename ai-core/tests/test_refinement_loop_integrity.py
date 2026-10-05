"""재검증 루프가 올바른 것을 고치고 올바르게 보고한다.

3개 레포 통합 테스트(Backend -> Redis -> AI -> Sandbox)에서 세 가지가 드러났다.

1. 라운드 기록이 백엔드로 가지 않는다. 서버는 iterationCount만 받아서 "두 번 돌았다"는
   알지만 무엇을 시도했는지 모른다.
2. 수집 오류일 때 Refiner에게 매니페스트를 주지 않는다. `pallets/itsdangerous`에서
   freezegun이 없어 깨졌는데, 모델이 가진 파일은 테스트 파일 하나였다.
3. 그래서 모델이 테스트 파일을 고치려 했다. 테스트를 약화시켜 통과시키는 경로다.
"""

import unittest

from app import events as event_builder
from app.agents.patching import inspect_diff
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


def _diff(*paths: str) -> str:
    out = []
    for p in paths:
        out.append(f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n")
    return "".join(out)


class RoundRecordReachesTheBackendTests(unittest.TestCase):
    def test_the_result_event_carries_the_round_records(self) -> None:
        state = AgentState(
            job_id="j", request_id="r", repository_url="https://github.com/o/r", status=JobStatus.failed
        )
        state.refine_rounds = [
            {"round": 1, "patch_bytes": 120, "before_judge_status": "Fail",
             "after_judge_status": "Fail", "sandbox_exit_code": 2, "failed_step": "smoke"}
        ]
        payload = event_builder.result_event(state)
        self.assertIn("refine_rounds", payload)
        self.assertEqual(payload["refine_rounds"][0]["round"], 1)
        self.assertEqual(payload["refine_rounds"][0]["after_judge_status"], "Fail")

    def test_a_run_without_rounds_sends_an_empty_list(self) -> None:
        state = AgentState(job_id="j", repository_url="https://github.com/o/r")
        self.assertEqual(event_builder.result_event(state)["refine_rounds"], [])


class TestPathsAreProtectedTests(unittest.TestCase):
    """테스트를 고쳐서 통과시키는 것은 우리가 팔려는 것의 반대다.

    모델이 더 좋아지면 확률은 내려가지만 0이 되지 않는다. .github/를 막는 것과 같은 이유로
    경로로 막는다. 테스트 자체에 버그가 있는 레포는 Refiner가 가이드 문장으로만 제안한다.
    """

    def test_a_python_test_file_is_rejected(self) -> None:
        verdict = inspect_diff(_diff("tests/test_timed.py"))
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "patch_touches_test_path")

    def test_a_nested_test_package_is_rejected(self) -> None:
        self.assertFalse(inspect_diff(_diff("tests/test_itsdangerous/test_timed.py")).accepted)

    def test_a_suffix_named_test_is_rejected(self) -> None:
        self.assertFalse(inspect_diff(_diff("app/cache_test.py")).accepted)

    def test_a_go_test_is_rejected(self) -> None:
        self.assertFalse(inspect_diff(_diff("internal/cache_test.go")).accepted)

    def test_a_java_test_tree_is_rejected(self) -> None:
        self.assertFalse(inspect_diff(_diff("src/test/java/com/x/CacheTest.java")).accepted)

    def test_a_javascript_spec_is_rejected(self) -> None:
        self.assertFalse(inspect_diff(_diff("src/cache.spec.ts")).accepted)
        self.assertFalse(inspect_diff(_diff("src/cache.test.js")).accepted)

    def test_a_patch_that_also_touches_a_test_is_rejected(self) -> None:
        # 섞어 보내면 통과하는 구멍을 두지 않는다.
        self.assertFalse(inspect_diff(_diff("requirements.txt", "tests/test_timed.py")).accepted)

    def test_production_code_is_still_accepted(self) -> None:
        verdict = inspect_diff(_diff("app/cache.py", "requirements.txt", "pyproject.toml"))
        self.assertTrue(verdict.accepted, verdict.reason)

    def test_a_path_that_merely_contains_the_word_test_is_accepted(self) -> None:
        # latest.py나 contest/ 같은 이름을 막으면 멀쩡한 수정이 거절된다.
        self.assertTrue(inspect_diff(_diff("app/latest.py")).accepted)
        self.assertTrue(inspect_diff(_diff("contest/views.py")).accepted)
        self.assertTrue(inspect_diff(_diff("app/testing_utils.py")).accepted)


class ManifestsReachTheRefinerOnCollectionErrorsTests(unittest.TestCase):
    """수집 오류는 대개 테스트 전용 의존성이 없는 것이다.

    고칠 자리는 의존성 선언인데 로그는 테스트 파일 경로만 말한다. 매니페스트를 함께 주지
    않으면 모델이 가진 파일은 테스트 파일뿐이고, 그러면 테스트를 고치려 한다.
    """

    def _state(self, category: str, exit_code: int, stderr: str) -> AgentState:
        state = AgentState(
            job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed
        )
        state.preflight_report = RepositoryPreflightReport(
            repository_url="https://github.com/o/r", cloneable=True, executable=True
        )
        state.execution_result = SandboxResult(exit_code=exit_code, stderr=stderr)
        state.judge_report = {"status": "Fail", "reason_category": category}
        return state

    def _collected_paths(self, state: AgentState) -> list[str]:
        from app.agents import source_context
        from app.workflow import repository_validation as workflow

        captured: dict = {}

        def _collect(url, paths, **kwargs):
            captured["paths"] = list(paths)
            return {}

        original = source_context.collect
        source_context.collect = _collect
        try:
            workflow.attach_source_files(state)
        finally:
            source_context.collect = original
        return captured.get("paths", [])

    def test_a_collection_error_gets_the_manifests(self) -> None:
        from app.agents import source_context

        paths = self._collected_paths(
            self._state(
                "test_failure",
                2,
                "FAILED tests/test_timed.py\nE   ModuleNotFoundError: No module named 'freezegun'",
            )
        )
        self.assertTrue(
            any(p in paths for p in source_context.MANIFEST_CANDIDATES), paths
        )

    def test_a_plain_test_failure_does_not_get_them(self) -> None:
        # exit 1은 테스트가 돌았고 실패한 것이다. 고칠 자리는 코드이지 매니페스트가 아니다.
        paths = self._collected_paths(
            self._state("test_failure", 1, "FAILED tests/test_cache.py::test_roundtrip - assert 1 == 2")
        )
        self.assertTrue(paths, "로그에서 경로를 찾지 못하면 이 검사가 의미를 잃는다")
        self.assertNotIn("pyproject.toml", paths)


if __name__ == "__main__":
    unittest.main()
