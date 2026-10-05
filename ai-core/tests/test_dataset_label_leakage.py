"""생성 데이터셋이 정답을 로그에 적어두지 않는다.

`sandbox_execution_for`가 stderr 기본값으로 `spaced(failure)`를 썼다. 라벨
`dockerfile_missing`인 케이스의 stderr가 `dockerfile missing`이었다. 610건 중 168건이
그랬다.

이건 정답 누출이다. 로그 문구를 읽는 판정자는 공짜로 맞히고, 종료 코드만 보는 규칙은
맞힐 수 없다. 그리고 로그를 읽는 쪽이 LLM 판정이 레포에 심어둔 지시에 속은 경로다
(docs/judge-policy.md 8절). 이 데이터로 모델을 학습시키면 로그를 믿는 쪽으로 당긴다.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from generate_dataset_batch import SANDBOX_FAILURES, sandbox_execution_for, spaced  # noqa: E402


class NoLabelLeakageTests(unittest.TestCase):
    def test_no_failure_type_appears_in_its_own_log(self) -> None:
        for failure in SANDBOX_FAILURES:
            with self.subTest(failure=failure):
                execution = sandbox_execution_for(failure, 1)
                log = f"{execution['stdout']}{execution['stderr']}".lower()
                self.assertNotIn(spaced(failure), log)
                self.assertNotIn(failure, log)

    def test_every_failure_type_still_has_a_log_to_read(self) -> None:
        # 누출을 없애려고 로그를 비우면 Critic과 Refiner가 볼 근거가 사라진다.
        for failure in SANDBOX_FAILURES:
            with self.subTest(failure=failure):
                execution = sandbox_execution_for(failure, 1)
                self.assertTrue(execution["stderr"].strip(), failure)

    # 낱말 겹침은 검사하지 않는다. sh는 "./gradlew: Permission denied"를, 파이썬은
    # "SyntaxError: invalid syntax"를 실제로 그렇게 찍는다. 그 낱말이 라벨과 겹치는 것은
    # 누출이 아니라 실제 신호다. 문제는 로그가 라벨을 풀어 쓴 것 외에 아무 정보도 담지
    # 않을 때이고, 그것은 위 두 검사가 잡는다.


class ExitCodeContractTests(unittest.TestCase):
    """종료 코드가 사유를 담는다. 규칙이 보는 신호는 이쪽이다."""

    def test_a_missing_manifest_uses_its_exit_code(self) -> None:
        self.assertEqual(sandbox_execution_for("dockerfile_missing", 1)["exit_code"], 86)

    def test_no_runner_uses_its_exit_code(self) -> None:
        for failure in ("unsupported_stack", "no_smoke_command"):
            self.assertEqual(sandbox_execution_for(failure, 1)["exit_code"], 87, failure)

    def test_nothing_to_verify_uses_its_exit_code(self) -> None:
        self.assertEqual(sandbox_execution_for("npm_test_missing", 1)["exit_code"], 89)

    def test_a_timeout_is_marked_as_one(self) -> None:
        execution = sandbox_execution_for("sandbox_timeout", 1)
        self.assertTrue(execution["timed_out"])
        self.assertIsNone(execution["exit_code"])

    def test_an_infra_failure_carries_the_signal_not_just_a_log(self) -> None:
        # 판정은 로그를 읽지 않는다. infra_error가 있어야 Error로 끝낼 수 있다.
        execution = sandbox_execution_for("docker_daemon_unavailable", 1)
        self.assertEqual(execution["infra_error"], "docker_daemon_unreachable")

    def test_a_repository_failure_carries_no_infra_signal(self) -> None:
        self.assertNotIn("infra_error", sandbox_execution_for("pytest_failure", 1))


if __name__ == "__main__":
    unittest.main()
