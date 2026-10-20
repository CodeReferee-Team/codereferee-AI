import unittest

from app.agents import nodes
from app.models import AgentState


def _state(root_cause: str, guidance: list[str]) -> AgentState:
    s = AgentState(job_id="t", repository_url="https://github.com/o/r")
    s.critic_feedback = {
        "issue": "The service never responded.",
        "root_cause": root_cause,
        "evidence": ["http_status=None"],
        "recommended_action": "Start the service.",
    }
    s.refiner_report = {
        "summary": "x",
        "patch_guidance": list(guidance),
        "verification_steps": ["re-run validation"],
        "risk": "medium",
        "edits": [],
    }
    return s


class RefinerHybridTests(unittest.TestCase):
    def test_injects_missing_service_surface(self) -> None:
        # LLM이 수정부위를 빠뜨린 경우: 규칙 스켈레톤이 health/endpoint/browser를 메운다.
        s = _state("The service did not answer HTTP requests.", ["Investigate the problem."])
        nodes._ensure_remediation_surface(s)
        text = " ".join(s.refiner_report["patch_guidance"]).casefold()
        self.assertIn("health", text)
        self.assertIn("endpoint", text)
        self.assertIn("browser", text)

    def test_idempotent(self) -> None:
        s = _state("The service did not answer HTTP requests.", ["Investigate."])
        nodes._ensure_remediation_surface(s)
        n1 = len(s.refiner_report["patch_guidance"])
        nodes._ensure_remediation_surface(s)
        self.assertEqual(len(s.refiner_report["patch_guidance"]), n1)

    def test_category_backstop_when_text_misses(self) -> None:
        # critic 텍스트가 수정부위 키워드를 안 가져도 판정 reason_category로 메운다.
        s = _state("npm reported a problem during install.", ["Look into it."])
        s.critic_feedback["issue"] = "A problem occurred during setup."
        s.critic_feedback["recommended_action"] = "Resolve it."
        s.judge_report = {"reason_category": "dependency_install_failed"}
        nodes._ensure_remediation_surface(s)
        text = " ".join(s.refiner_report["patch_guidance"]).casefold()
        self.assertIn("exit_code=0", text)
        self.assertIn("failing", text)

    def test_keeps_llm_guidance_first(self) -> None:
        # LLM 서술은 보존(앞), 규칙 surface는 뒤에 보강.
        s = _state("The service did not answer HTTP requests.", ["Expose a readiness endpoint on the right port."])
        nodes._ensure_remediation_surface(s)
        self.assertEqual(s.refiner_report["patch_guidance"][0], "Expose a readiness endpoint on the right port.")


if __name__ == "__main__":
    unittest.main()
