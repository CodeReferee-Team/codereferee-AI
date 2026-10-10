import unittest

from app.models import AgentState
from evals.narrative import score_narrative


def _state(critic=None, refiner=None) -> AgentState:
    s = AgentState(job_id="t", repository_url="https://github.com/o/r")
    if critic is not None:
        s.critic_feedback = critic
    if refiner is not None:
        s.refiner_report = refiner
    return s


VALID_CRITIC = {
    "issue": "The service cannot survive a pod failure.",
    "root_cause": "The deployment runs a single replica, so the one pod dying takes the service down.",
    "evidence": ["chaos.replicas=1"],
    "recommended_action": "Run more than one replica.",
}


class NarrativeScoringTests(unittest.TestCase):
    def test_critic_concept_covered_passes(self) -> None:
        ann = {"critic_concepts": [["replica"]]}
        out = score_narrative(ann, _state(critic=VALID_CRITIC), packet_text="chaos.replicas=1")
        self.assertIsNotNone(out)
        self.assertTrue(out["critic"]["passed"], out["critic"]["failures"])
        self.assertTrue(out["critic"]["schema_ok"])

    def test_critic_missing_concept_fails(self) -> None:
        ann = {"critic_concepts": [["replica"]]}
        latency = {
            "issue": "The service is slow.",
            "root_cause": "The p95 latency exceeds the budget.",
            "evidence": ["p95_latency_ms=2000"],
            "recommended_action": "Reduce request handling time.",
        }
        out = score_narrative(ann, _state(critic=latency), packet_text="p95_latency_ms=2000")
        self.assertFalse(out["critic"]["passed"])

    def test_broken_report_recorded_not_raised(self) -> None:
        # 깨진 critic 리포트(필수 키 누락)는 eval을 죽이지 않고 schema_ok=False로 기록된다.
        out = score_narrative({"critic_concepts": [["replica"]]}, _state(critic={"issue": "x"}), "p")
        self.assertFalse(out["critic"]["schema_ok"])
        self.assertFalse(out["critic"]["passed"])

    def test_no_reports_returns_none(self) -> None:
        self.assertIsNone(score_narrative({"critic_concepts": [["replica"]]}, _state(), "p"))


if __name__ == "__main__":
    unittest.main()
