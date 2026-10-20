import unittest
from unittest import mock

from app.agents import nodes
from app.agents.evidence import build_refiner_evidence
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


def _state() -> AgentState:
    state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
    state.preflight_report = RepositoryPreflightReport(
        repository_url="https://github.com/o/r", cloneable=True, executable=True
    )
    state.execution_result = SandboxResult(exit_code=1, stderr="SyntaxError: invalid syntax")
    state.judge_report = {"status": "Fail", "reason_category": "sandbox_nonzero_exit", "reason": "syntax error"}
    state.critic_feedback = {"root_cause": "broken def", "recommended_action": "fix it"}
    state.source_files = {"calc.py": "def add(a, b):\n    return a - b\n"}
    return state


class RefinerEvidenceTests(unittest.TestCase):
    """Refiner에게는 편집을 쓰는 데 필요한 것만 준다. 8B가 sre_metrics를 리포트로 되받아 적었다."""

    def test_irrelevant_sections_are_dropped(self) -> None:
        packet = build_refiner_evidence(_state())
        for dropped in ("sre_metrics", "evidence_refs", "secondary_signals", "metrics"):
            self.assertNotIn(dropped, packet)

    def test_the_parts_needed_to_write_an_edit_are_present(self) -> None:
        packet = build_refiner_evidence(_state())
        self.assertEqual(packet["judge"]["reason_category"], "sandbox_nonzero_exit")
        self.assertIn("calc.py", packet["source_files"])
        self.assertIn("log_excerpt", packet["execution"])

    def test_it_is_much_smaller_than_the_full_packet(self) -> None:
        import json

        from app.agents.evidence import build_evidence_packet

        state = _state()
        full = len(json.dumps(build_evidence_packet(state), ensure_ascii=False))
        trimmed = len(json.dumps(build_refiner_evidence(state), ensure_ascii=False))
        self.assertLess(trimmed, full)


class RetryOnRejectionTests(unittest.TestCase):
    """거부 이유는 우리가 파일과 대조해 만든 결정적 신호다. 한 번만 다시 묻는다."""

    def setUp(self) -> None:
        self._enabled = nodes.llm.enabled
        nodes.llm.enabled = True

    def tearDown(self) -> None:
        nodes.llm.enabled = self._enabled

    def _report(self, find: str) -> dict:
        return {
            "summary": "fix",
            "edits": [{"path": "calc.py", "find": [find], "replace": ["    return a + b"]}],
            "patch_guidance": ["fix"],
            "verification_steps": ["rerun"],
            "risk": "low",
        }

    def test_bad_anchor_triggers_one_retry_that_can_succeed(self) -> None:
        state = _state()
        good = self._report("    return a - b")
        with mock.patch.object(nodes, "_invoke_validated_report", side_effect=[self._report("nope"), good]) as call:
            nodes.refiner_node(state)
        self.assertEqual(call.call_count, 2)
        self.assertIn("+    return a + b", state.refiner_report["patch_diff"])
        self.assertTrue(any("retry produced an applicable edit" in e for e in state.events))

    def test_retry_happens_at_most_once(self) -> None:
        state = _state()
        # 리포트 dict는 호출마다 새로 만든다. _diff_from_edits가 edits를 pop하므로
        # 같은 객체를 돌려주면 두 번째 호출의 edits가 이미 사라진 상태가 된다.
        with mock.patch.object(
            nodes, "_invoke_validated_report", side_effect=[self._report("nope"), self._report("nope")]
        ) as call:
            nodes.refiner_node(state)
        self.assertEqual(call.call_count, 2)
        self.assertIsNone(state.refiner_report.get("patch_diff"))
        self.assertTrue(any("retry still rejected" in e for e in state.events))

    def test_an_accepted_edit_does_not_trigger_a_retry(self) -> None:
        state = _state()
        with mock.patch.object(
            nodes, "_invoke_validated_report", return_value=self._report("    return a - b")
        ) as call:
            nodes.refiner_node(state)
        self.assertEqual(call.call_count, 1)

    def test_no_edits_at_all_does_not_trigger_a_retry(self) -> None:
        # 앵커 문제가 아니라 모델이 아무것도 내지 않은 경우다. 다시 물어도 같은 일이 생긴다.
        state = _state()
        empty = {"summary": "nothing", "edits": None, "patch_guidance": ["x"],
                 "verification_steps": ["y"], "risk": "low"}
        with mock.patch.object(nodes, "_invoke_validated_report", return_value=empty) as call:
            nodes.refiner_node(state)
        self.assertEqual(call.call_count, 1)

    def test_retry_is_skipped_when_the_llm_is_off(self) -> None:
        nodes.llm.enabled = False
        state = _state()
        nodes.refiner_node(state)
        self.assertFalse(any("retrying edits" in e for e in state.events))
