"""세부사항 3호 — 레포/브랜치 접근 실패는 코드 결함이 아니다.

50개 레포 스윕에서 ref_not_found 8건·repository_not_found 1건이 전부 FAILED로 나왔다.
브랜치가 없거나 레포가 404거나 비공개면 우리가 코드를 아예 못 받은 것이다. 코드가
깨진 게 아니라 URL·브랜치·접근 문제이므로 판정 불가(ERROR)여야 한다.

preflight 실패의 사유 카테고리가 repository_validation의 UNVERIFIABLE_CATEGORIES에
들어 판정 불가로 라우팅되는지 본다(라우팅 자체는 세부사항 1·2호 경로와 동일).
"""

import unittest

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport
from app.workflow.repository_validation import UNVERIFIABLE_CATEGORIES


def _preflight_fail_state(reason: str) -> AgentState:
    s = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.running)
    s.preflight_report = RepositoryPreflightReport(
        repository_url="https://github.com/o/r", cloneable=False, executable=False,
        reason=reason, evidence=[reason],
    )
    return s


class PreflightAccessIsNotACodeDefectTests(unittest.TestCase):
    # 실제 preflight가 남기는 사유 문장들. 정확한 세부 카테고리(_preflight_category의
    # 우선순위)보다 "코드 결함이 아니라 판정 불가로 라우팅되는가"가 3호의 불변식이다.
    REASONS = [
        "Repository not found (404).",
        "fatal: couldn't find remote ref main",
        "Repository is private; authentication required.",
        "Invalid repository URL.",
        "Could not clone the repository.",
    ]

    def test_each_access_failure_routes_to_unverifiable(self) -> None:
        for reason in self.REASONS:
            with self.subTest(reason=reason):
                report = nodes._fallback_judge(_preflight_fail_state(reason))
                self.assertEqual(report["status"], "Fail")  # judge 자체는 Fail 카테고리로 분류
                # 그 카테고리가 UNVERIFIABLE이면 워크플로가 판정 불가(ERROR)로 바꾸고
                # Critic/Refiner를 건너뛴다(세부사항 1·2호와 동일 라우팅).
                self.assertIn(report["reason_category"], UNVERIFIABLE_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
