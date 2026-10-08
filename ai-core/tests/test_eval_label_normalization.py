"""생성 데이터셋 라벨을 정식 사유 코드로 옮긴다.

데이터셋은 우리 reason_category보다 잘게 라벨링했고 같은 뜻에 이름이 둘인 것도 있다.
이름만 다른 것은 옮기고, 우리 분류로 표현할 수 없는 것은 그대로 두고 틀린 것으로 센다.
억지로 옮기면 정답을 우리가 정하는 셈이 된다.
"""

import json
import unittest

from evals import cases


def _row(case_id: str) -> dict:
    path = cases.SYNTHETIC_SLICES["T1-sandbox"]
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line)["case_id"] == case_id:
            return json.loads(line)
    raise AssertionError(f"없는 케이스: {case_id}")


class SynonymTests(unittest.TestCase):
    def test_a_rename_lands_on_the_canonical_code(self) -> None:
        self.assertEqual(cases.CATEGORY_SYNONYMS["sandbox_timeout"], "timeout")
        self.assertEqual(cases.CATEGORY_SYNONYMS["pytest_failure"], "test_failure")

    def test_the_two_spellings_of_one_thing_collapse(self) -> None:
        self.assertEqual(
            cases.CATEGORY_SYNONYMS["dockerfile_missing"],
            cases.CATEGORY_SYNONYMS["missing_dockerfile"],
        )

    def test_every_synonym_target_is_a_real_code(self) -> None:
        from app.agents.schemas import REASON_CATEGORIES

        for source, target in cases.CATEGORY_SYNONYMS.items():
            self.assertIn(target, REASON_CATEGORIES, f"{source} -> {target}")

    def test_unmapped_labels_are_left_alone(self) -> None:
        # 옮길 자리가 없으면 그대로 둔다. 통과율을 올리려고 정식 코드에 끼워 넣지 않는다.
        for label in cases.UNMAPPED_CATEGORIES:
            self.assertNotIn(label, cases.CATEGORY_SYNONYMS)


class InfraSignalTests(unittest.TestCase):
    """판정이 운영에서 받는 입력을 평가에서도 받아야 한다."""

    def test_an_infra_label_carries_the_infra_error_signal(self) -> None:
        # 운영 경로는 docker 데몬에 닿지 못하면 infra_error를 채운다. 데이터셋은 그 필드가
        # 생기기 전에 만들어져 비어 있어서, 평가에서만 Error가 Fail로 나왔다.
        case = cases._sandbox_failure_case(_row("SANDBOX-GEN-001"))
        self.assertEqual(case.expected["verdict"], "Error")
        self.assertEqual(case.raw_state["execution_result"]["infra_error"], "sandbox_environment_error")

    def test_the_original_row_is_not_mutated(self) -> None:
        row = _row("SANDBOX-GEN-001")
        cases._sandbox_failure_case(row)
        self.assertNotIn("infra_error", row["execution_result"])

    def test_a_repository_failure_gets_no_infra_signal(self) -> None:
        case = cases._sandbox_failure_case(_row("SANDBOX-20260719-051"))
        self.assertNotIn("infra_error", case.raw_state["execution_result"])



def _metric_row(label: str) -> dict:
    path = cases.SYNTHETIC_SLICES["T1-metrics"]
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line)["expected_reason_category"] == label:
            return json.loads(line)
    raise AssertionError(f"라벨 없는 케이스: {label}")


class MetricSynonymTests(unittest.TestCase):
    """metrics 슬라이스도 sandbox처럼 라벨을 정식 코드로 옮긴다.

    sandbox 빌더는 CATEGORY_SYNONYMS를 적용했지만 metrics 빌더는 생짜 라벨을 써서
    boundary_fail 같은 이름이 전부 틀린 것으로 세어졌다. 매핑 근거는 각 케이스가
    실제로 깨뜨리는 단일 SLO와 Judge의 1순위 출력이다(둘이 100% 일치함을 확인).
    """

    def test_every_metric_alias_target_is_a_real_code(self) -> None:
        from app.agents.schemas import REASON_CATEGORIES

        for source, target in cases.METRIC_LABEL_ALIASES.items():
            self.assertIn(target, REASON_CATEGORIES, f"{source} -> {target}")

    def test_a_pass_alias_lands_on_all_checks_passed(self) -> None:
        self.assertEqual(cases.METRIC_LABEL_ALIASES["all_slo_passed"], "all_checks_passed")
        self.assertEqual(cases.METRIC_LABEL_ALIASES["latency_boundary_fail"], "latency_slo_violation")

    def test_loaded_case_category_is_normalized(self) -> None:
        case = cases._metrics_judge_case(_metric_row("all_slo_passed"))
        self.assertEqual(case.expected["category"], "all_checks_passed")

    def test_every_metric_label_is_covered(self) -> None:
        # 정식 코드이거나, alias로 옮기거나, 명시적으로 보류한 것이어야 한다.
        # 셋 중 어디에도 없는 라벨은 조용히 틀린 것으로 세어지므로 드러낸다.
        from app.agents.schemas import REASON_CATEGORIES

        path = cases.SYNTHETIC_SLICES["T1-metrics"]
        labels = {
            json.loads(line)["expected_reason_category"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        known = set(REASON_CATEGORIES) | set(cases.METRIC_LABEL_ALIASES) | cases.METRIC_UNMAPPED
        self.assertEqual(labels - known, set())

    def test_deferred_labels_are_left_alone(self) -> None:
        # 다중 위반은 Judge가 하나만 고른다. 대표 카테고리는 SLO 우선순위 정책이라
        # 점수용 매핑에 끼워 넣지 않고 보류한 채 틀린 것으로 센다.
        self.assertNotIn("multiple_slo_violations", cases.METRIC_LABEL_ALIASES)
        case = cases._metrics_judge_case(_metric_row("multiple_slo_violations"))
        self.assertEqual(case.expected["category"], "multiple_slo_violations")
        self.assertEqual(case.note, "category_unmapped=multiple_slo_violations")

if __name__ == "__main__":
    unittest.main()
