import shutil
import unittest

from app.agents import nodes
from evals import repair


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git과 sh가 필요하다")
class RepairEvalTests(unittest.TestCase):
    """수리율 평가의 하한(none)과 상한(oracle)이 기대대로 나오는지 본다. 하네스가 틀리면 모델 점수도 틀린다."""

    def setUp(self) -> None:
        self._enabled = nodes.llm.enabled
        nodes.llm.enabled = False

    def tearDown(self) -> None:
        nodes.llm.enabled = self._enabled

    def _case(self, case_id: str) -> dict:
        return repair.load_cases([case_id])[0]

    def test_oracle_edit_repairs_and_keeps_the_submitted_verdict(self) -> None:
        row = repair.run_case(self._case("off-by-one-pagination"), "oracle")
        self.assertEqual(row["initial_verdict"], "Fail")
        self.assertTrue(row["target_attached"])
        self.assertTrue(row["repaired"])
        # 패치가 통과해도 제출된 레포는 불합격이다.
        self.assertEqual(row["final_verdict"], "Fail")

    def test_compile_failure_is_detected_and_repaired(self) -> None:
        row = repair.run_case(self._case("syntax-missing-colon"), "oracle")
        self.assertEqual(row["initial_category"], "compile_failed")
        self.assertTrue(row["repaired"])

    def test_rule_fallback_produces_no_patch(self) -> None:
        row = repair.run_case(self._case("keyerror-missing-default"), "none")
        self.assertFalse(row["patch_produced"])
        self.assertFalse(row["repaired"])

    def test_editing_the_test_instead_of_the_code_is_not_a_repair(self) -> None:
        case = self._case("keyerror-missing-default")
        case["oracle_edits"] = [{
            "path": "tests/test_app.py",
            "find": ['    assert quota({"name": "free"}) == 10'],
            "replace": ["    pass"],
        }]
        row = repair.run_case(case, "oracle")
        self.assertFalse(row["repaired"])


if __name__ == "__main__":
    unittest.main()
