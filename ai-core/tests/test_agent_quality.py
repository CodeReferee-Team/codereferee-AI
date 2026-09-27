import unittest
from unittest.mock import patch

from app.agents import nodes
from app.agents.evidence import build_evidence_packet
from app.agents.schemas import CriticReport, JudgeReport, PlannerReport, RefinerReport, validate_report
from app.models import AgentState, RepositoryPreflightReport, SandboxResult
from tests.agent_quality import evaluate_cases, load_cases, run_case


class AgentQualityTests(unittest.TestCase):
    def test_strict_report_schemas_reject_empty_or_extra_fields(self) -> None:
        valid_judge = {"status": "Fail", "reason_category": "timeout", "reason": "timeout", "evidence": ["timed_out=True"]}
        self.assertEqual(validate_report(JudgeReport, valid_judge)["status"], "Fail")
        with self.assertRaises(Exception):
            validate_report(JudgeReport, {"status": "Pass", "reason_category": "all_checks_passed", "reason": "", "evidence": []})
        with self.assertRaises(Exception):
            validate_report(JudgeReport, {"status": "Pass", "reason_category": "all_checks_passed", "reason": "ok", "evidence": ["ok"], "extra": "no"})
        with self.assertRaises(Exception):
            validate_report(RefinerReport, {"summary": "x", "patch_guidance": ["x"], "verification_steps": ["x"], "risk": "urgent"})

    def test_all_fallback_agent_outputs_satisfy_strict_schemas(self) -> None:
        for case in load_cases():
            with self.subTest(case=case["id"]):
                state = run_case(case)
                validate_report(PlannerReport, state.validation_plan)
                validate_report(JudgeReport, state.judge_report)
                validate_report(CriticReport, state.critic_feedback)
                validate_report(RefinerReport, state.refiner_report)
                self.assertIsInstance(state.validation_plan, dict)
                self.assertIsInstance(state.judge_report, dict)
                self.assertIsInstance(state.critic_feedback, dict)
                self.assertIsInstance(state.refiner_report, dict)

    def test_golden_agent_quality_scores_reach_minimum_gate(self) -> None:
        result = evaluate_cases()
        for agent, score in result["scores"].items():
            with self.subTest(agent=agent):
                self.assertGreaterEqual(score, 0.80, result)

    def test_evidence_packet_exposes_failure_category_and_evidence_refs(self) -> None:
        state = AgentState(
            job_id="evidence",
            repository_url="https://github.com/example/web.git",
            preflight_report=RepositoryPreflightReport(
                repository_url="https://github.com/example/web.git",
                cloneable=True,
                executable=True,
                reason="reachable",
            ),
            execution_result=SandboxResult(
                exit_code=0,
                stdout="server returned HTTP 500",
                http_status=500,
                browser_loaded=False,
                service_check_attempted=True,
                browser_check_attempted=True,
            ),
        )
        packet = build_evidence_packet(state)
        self.assertEqual(packet["schema_version"], "agent-evidence.v2")
        self.assertEqual(packet["failure_category"], "service_failure")
        self.assertIn("exec.http_status", packet["evidence_refs"])
        self.assertIn("http_status=500", packet["primary_signal"])

    def test_invalid_llm_report_gets_one_schema_repair_before_fallback(self) -> None:
        # 판정은 규칙이 하므로 LLM 경로는 Critic으로 확인한다.
        class FakeLLM:
            enabled = True

            def __init__(self) -> None:
                self.repairs = 0

            def invoke_text(self, *_args, **_kwargs):
                return '{"status":"Maybe","reason":"","evidence":[]}'

            def invoke_schema_repair(self, **_kwargs):
                self.repairs += 1
                return (
                    '{"issue":"sandbox failed","root_cause":"dependency install failed",'
                    '"evidence":["pip install failed"],"recommended_action":"pin dependency versions"}'
                )

        fake = FakeLLM()
        state = AgentState(job_id="repair", repository_url="https://github.com/example/repo.git")
        with patch.object(nodes, "llm", fake):
            result = nodes.critic_node(state)

        self.assertEqual(fake.repairs, 1)
        self.assertEqual(result.critic_feedback["root_cause"], "dependency install failed")
        self.assertIn("Critic: Agent schema repair accepted", result.events)


if __name__ == "__main__":
    unittest.main()
