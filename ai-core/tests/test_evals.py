import unittest

from evals import cases as eval_cases
from evals import metrics as eval_metrics


class WilsonIntervalTests(unittest.TestCase):
    def test_interval_brackets_the_point_estimate(self) -> None:
        value, (low, high) = eval_metrics.proportion(18, 20)
        self.assertAlmostEqual(value, 0.9)
        self.assertLess(low, 0.9)
        self.assertGreater(high, 0.9)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)

    def test_small_sample_interval_is_wide(self) -> None:
        # golden 20건에서는 1건 차이가 5%p다. 구간이 좁게 나오면 우연을 개선으로 착각한다.
        _, (low, _high) = eval_metrics.proportion(20, 20)
        self.assertLess(low, 0.90)

    def test_zero_denominator_is_none(self) -> None:
        value, interval = eval_metrics.proportion(0, 0)
        self.assertIsNone(value)
        self.assertIsNone(interval)


class VerdictMetricTests(unittest.TestCase):
    def _summary(self, pairs):
        return eval_metrics.verdict_summary([{"expected": e, "actual": a} for e, a in pairs])

    def test_false_pass_counts_any_non_pass_label_given_pass(self) -> None:
        s = self._summary([("Fail", "Pass"), ("Error", "Pass"), ("Fail", "Fail"), ("Pass", "Pass")])
        self.assertEqual(s["false_pass"]["n"], 3)  # 정답이 Pass가 아닌 3건이 분모
        self.assertAlmostEqual(s["false_pass"]["value"], 2 / 3)

    def test_error_reported_as_fail_is_tracked_separately(self) -> None:
        s = self._summary([("Error", "Fail"), ("Error", "Error")])
        self.assertAlmostEqual(s["error_as_fail"]["value"], 0.5)

    def test_false_fail_covers_fail_and_error_predictions(self) -> None:
        s = self._summary([("Pass", "Fail"), ("Pass", "Error"), ("Pass", "Pass")])
        self.assertAlmostEqual(s["false_fail"]["value"], 2 / 3)

    def test_confusion_matrix_counts_every_pair(self) -> None:
        s = self._summary([("Fail", "Pass"), ("Fail", "Fail")])
        self.assertEqual(s["confusion"]["Fail"]["Pass"], 1)
        self.assertEqual(s["confusion"]["Fail"]["Fail"], 1)

    def test_ambiguous_labels_are_excluded_from_accuracy(self) -> None:
        rows = [{"expected": "Fail", "actual": "Fail"}, {"expected": "Fail", "actual": "Pass", "ambiguous": True}]
        s = eval_metrics.verdict_summary(rows)
        self.assertEqual(s["accuracy"]["n"], 1)
        self.assertAlmostEqual(s["accuracy"]["value"], 1.0)


class CategoryMetricTests(unittest.TestCase):
    def test_macro_f1_weights_rare_categories_equally(self) -> None:
        rows = [
            {"expected": "timeout", "actual": "timeout"},
            {"expected": "timeout", "actual": "timeout"},
            {"expected": "rare_case", "actual": "timeout"},
        ]
        s = eval_metrics.category_summary(rows)
        self.assertAlmostEqual(s["accuracy"]["value"], 2 / 3)
        self.assertLess(s["macro_f1"], s["accuracy"]["value"])  # 드문 카테고리 실패가 그대로 드러난다

    def test_top_confusions_lists_most_common_mistakes(self) -> None:
        rows = [{"expected": "a", "actual": "b"}, {"expected": "a", "actual": "b"}, {"expected": "c", "actual": "d"}]
        s = eval_metrics.category_summary(rows)
        self.assertEqual(s["top_confusions"][0], {"expected": "a", "actual": "b", "count": 2})


class ConsistencyTests(unittest.TestCase):
    def test_majority_verdict_and_agreement(self) -> None:
        self.assertEqual(eval_metrics.majority(["Pass", "Fail", "Pass"]), "Pass")
        self.assertAlmostEqual(eval_metrics.agreement(["Pass", "Fail", "Pass"]), 2 / 3)
        self.assertAlmostEqual(eval_metrics.agreement(["Fail", "Fail", "Fail"]), 1.0)


class CaseLoadingTests(unittest.TestCase):
    def test_every_human_written_case_carries_a_label(self) -> None:
        loaded = eval_cases.load(["T0", "T0-adv", "T1-chaos"])
        self.assertEqual(len(loaded), 44)
        for case in loaded:
            with self.subTest(case=case.id):
                self.assertIn(case.expected["verdict"], ("Pass", "Fail", "Error"))
                self.assertTrue(case.expected["category"])
                self.assertIn(case.slice, ("T0", "T0-adv", "T1-chaos"))

    def test_state_is_rebuilt_from_fixture(self) -> None:
        case = next(c for c in eval_cases.load(["T1-chaos"]) if c.id == "chaos_not_recovered")
        state = case.build_state()
        self.assertIs(state.execution_result.chaos_observation["recovered"], False)
        self.assertEqual(state.sre_metrics.chaos.scenario, "pod_kill")

    def test_stratified_sampling_is_deterministic_and_covers_categories(self) -> None:
        first = eval_cases.load(["T1-sandbox"], per_category=2, seed=7)
        second = eval_cases.load(["T1-sandbox"], per_category=2, seed=7)
        self.assertEqual([c.id for c in first], [c.id for c in second])
        counts: dict[str, int] = {}
        for c in first:
            counts[c.expected["category"]] = counts.get(c.expected["category"], 0) + 1
        self.assertTrue(counts)
        self.assertTrue(all(n <= 2 for n in counts.values()))


if __name__ == "__main__":
    unittest.main()
