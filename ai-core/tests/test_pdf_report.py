"""PDF 추천 리포트 생성기. AI 산출(dict) → 사람이 읽는 PDF.

실제 PDF 바이트를 만들어 헤더·크기를 확인한다(한글 폰트 렌더 포함). report_from_state는
AgentState에서 dict를 뽑는 얇은 어댑터라 함께 본다.
"""
import tempfile
import unittest
from pathlib import Path

from app.models import AgentState, JobStatus
from app.report import build_pdf_report, report_from_state

REPORT = {
    "repository_url": "https://github.com/o/r",
    "judge": {
        "status": "Fail",
        "reason_category": "chaos_recovery_exceeds_expected_bound",
        "reason": "recovery 37.8s exceeds the bound 22.0s.",
        "evidence": ["chaos.replicas=1"],
    },
    "critic": {
        "issue": "서비스가 pod 장애를 무중단으로 견디지 못한다.",
        "root_cause": "단일 replica라 그 pod가 죽으면 전체 서비스가 중단된다.",
        "evidence": ["chaos.replicas=1"],
        "recommended_action": "replica를 2개 이상으로 둔다.",
    },
    "refiner": {
        "summary": "단일 replica 배포는 pod 장애 시 가용성을 모두 잃는다.",
        "patch_guidance": ["replicas를 올린다."],
        "verification_steps": ["container-kill 재실행으로 가용성 확인."],
        "risk": "low",
        "edits": [{"path": ".codereferee/validation.yaml", "find": "replicas: 1", "replace": "replicas: 2"}],
    },
}


class BuildPdfReportTests(unittest.TestCase):
    def test_produces_a_valid_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = build_pdf_report(REPORT, Path(tmp) / "r.pdf")
            data = out.read_bytes()
        self.assertEqual(data[:5], b"%PDF-")
        self.assertGreater(len(data), 2048)

    def test_handles_empty_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = build_pdf_report({"repository_url": "https://github.com/o/r"}, Path(tmp) / "r.pdf")
            self.assertEqual(out.read_bytes()[:5], b"%PDF-")


class ReportFromStateTests(unittest.TestCase):
    def test_extracts_sections(self) -> None:
        state = AgentState(job_id="j", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.judge_report = {"status": "Fail", "reason_category": "chaos_recovery_exceeds_expected_bound"}
        state.critic_feedback = {"root_cause": "single replica"}
        state.refiner_report = {"summary": "add replicas", "edits": []}
        report = report_from_state(state)
        self.assertEqual(report["repository_url"], "https://github.com/o/r")
        self.assertEqual(report["judge"]["reason_category"], "chaos_recovery_exceeds_expected_bound")
        self.assertEqual(report["critic"]["root_cause"], "single replica")


if __name__ == "__main__":
    unittest.main()
