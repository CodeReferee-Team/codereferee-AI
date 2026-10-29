"""검증 파이프라인이 LangGraph StateGraph로 돈다.

동작은 그래프 전환 전과 같아야 한다. 판정·이벤트·라운드는 test_repository_validation이 지키고,
여기서는 그래프의 모양(어느 노드가 어디로 가는가)과 그래프라서 새로 생기는 위험을 본다.
"""

import unittest
from unittest.mock import patch

from app.agents import source_context
from app.config import get_settings
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.workflow import repository_validation as workflow


class RecordingQueue:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


PREFLIGHT = RepositoryPreflightReport(
    repository_url="https://github.com/example/project",
    normalized_url="https://github.com/example/project",
    cloneable=True,
    executable=True,
    resolved_commit_sha="abc123",
)


def _chaos_failure() -> SandboxResult:
    return SandboxResult(
        exit_code=0, stderr="pod did not recover", duration_ms=5000, observation_status="observed",
        metrics={"availability": 0.99, "error_rate": 0.01, "p95_latency_ms": 20.0, "recovery_seconds": 3.42},
        chaos_observation={"type": "pod_kill", "recovered": False},
        baseline={"metrics": {"availability": 1.0, "p95_latency_ms": 12.0}},
    )


class GraphShapeTests(unittest.TestCase):
    def test_the_pipeline_nodes_are_graph_nodes(self) -> None:
        nodes = set(workflow.validation_graph().get_graph().nodes)
        for name in ("preflight", "planner", "sandbox", "skip_sandbox", "infra_gate",
                     "judge", "critic", "refiner", "refine", "finalize"):
            self.assertIn(name, nodes)

    def test_the_refinement_loop_is_its_own_graph(self) -> None:
        nodes = set(workflow.refinement_graph().get_graph().nodes)
        for name in ("refine_start", "refine_prepare", "refine_rerun", "refine_judge",
                     "refine_regenerate", "refine_exhausted", "refine_restore"):
            self.assertIn(name, nodes)

    def test_an_unjudgeable_run_skips_critic_and_refiner(self) -> None:
        # 판정 불가에서 Critic으로 가는 간선이 생기면 멀쩡한 코드를 고치려 든다(judge-policy 3.0절).
        edges = {(edge.source, edge.target) for edge in workflow.validation_graph().get_graph().edges}
        self.assertIn(("infra_gate", "finalize"), edges)
        self.assertIn(("judge", "finalize"), edges)
        self.assertNotIn(("infra_gate", "critic"), edges)

    def test_the_graph_can_be_drawn(self) -> None:
        mermaid = workflow.validation_graph().get_graph(xray=1).draw_mermaid()
        self.assertIn("refine_rerun", mermaid)


class GraphRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._collect = patch.object(source_context, "collect", return_value={})
        self._collect.start()

    def tearDown(self) -> None:
        self._collect.stop()

    def test_many_rounds_do_not_hit_the_recursion_limit(self) -> None:
        # 라운드마다 노드를 여러 개 지난다. LangGraph 기본 recursion_limit(25)에 걸려 예외로
        # 끝나면 설정으로 라운드를 늘린 운영자는 결과 이벤트를 받지 못한다.
        rounds = 8
        settings = get_settings().model_copy(update={"max_self_healing_retries": rounds})
        diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"

        def _refiner(state):
            state.refiner_report = {"summary": "s", "patch_guidance": ["g"], "verification_steps": ["v"],
                                    "risk": "low", "patch_diff": diff}
            return state

        calls: list[str | None] = []

        def _sandbox(repository_url, branch=None, commit_sha=None, patch_diff=None, **kwargs):
            calls.append(patch_diff)
            return _chaos_failure()

        state = AgentState(job_id="many-rounds", request_id="many-rounds",
                           repository_url="https://github.com/example/project")
        queue = RecordingQueue()
        with patch.object(workflow, "get_settings", return_value=settings), \
                patch.object(workflow.repository_preflight_runner, "run", return_value=PREFLIGHT), \
                patch.object(workflow.sandbox_runner, "run_repository", side_effect=_sandbox), \
                patch.object(workflow, "refiner_node", side_effect=_refiner), \
                patch.object(workflow, "record_validation_artifacts"):
            state = workflow.execute_repository_validation(state, output_queue=queue)

        self.assertEqual(len(calls), rounds + 1)
        self.assertEqual(len(state.refine_rounds), rounds)
        self.assertEqual(state.status, JobStatus.failed)
        results = [event for event in queue.published if event.get("type") == "result"]
        self.assertEqual(len(results), 1)

    def test_the_refinement_wrapper_still_runs_alone(self) -> None:
        # evals/repair.py가 루프만 따로 돌린다. 그래프로 옮겨도 같은 진입점을 남긴다.
        state = AgentState(job_id="alone", repository_url="https://github.com/example/project",
                           status=JobStatus.success, preflight_report=PREFLIGHT)
        state = workflow._run_refinement_rounds(state, lambda *args, **kwargs: None)
        self.assertEqual(state.status, JobStatus.success)
        self.assertFalse(state.metrics["patch_verified"])
        self.assertEqual(state.refine_rounds, [])


if __name__ == "__main__":
    unittest.main()
