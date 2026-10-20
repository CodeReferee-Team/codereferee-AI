import unittest

from app.agents import nodes
from app.models import SandboxResult

PREAMBLE = """[CodeReferee] preparing sandbox
[CodeReferee] cloning repository
Cloning into '/tmp/repository'...
[CodeReferee] detecting project stack
detected_stack=python
[CodeReferee] installing dependencies
"""


class FailureDetailTests(unittest.TestCase):
    """단계 이름만으로는 무엇이 잘못됐는지 알 수 없다. 사용자도, Refiner도 그렇다."""

    def _result(self, log: str, **kwargs) -> SandboxResult:
        report = {"detected_stack": "python", "failed_step": "dependencies", **kwargs}
        return SandboxResult(exit_code=1, stderr=log, sandbox_report=report)

    def test_reason_carries_the_failing_message(self) -> None:
        log = PREAMBLE + "ERROR: No matching distribution found for nope-zzz\n"
        reason = nodes._sandbox_failure_reason(self._result(log))
        self.assertIn("dependencies", reason)
        self.assertIn("No matching distribution found for nope-zzz", reason)

    def test_stage_markers_are_not_mistaken_for_the_message(self) -> None:
        log = PREAMBLE
        self.assertEqual(nodes._failure_detail(self._result(log)), "")

    def test_detail_is_bounded(self) -> None:
        log = PREAMBLE + "E" * 500
        self.assertLessEqual(len(nodes._failure_detail(self._result(log))), nodes.MAX_FAILURE_DETAIL_CHARS)

    def test_stdout_is_used_when_stderr_has_nothing(self) -> None:
        result = SandboxResult(exit_code=1, stdout=PREAMBLE + "make: *** [test] Error 1\n",
                               sandbox_report={"failed_step": "smoke"})
        self.assertIn("[test] Error 1", nodes._sandbox_failure_reason(result))

    def test_reason_without_any_log_still_names_the_step(self) -> None:
        reason = nodes._sandbox_failure_reason(self._result(""))
        self.assertIn("dependencies", reason)
        self.assertTrue(reason.endswith("."))
