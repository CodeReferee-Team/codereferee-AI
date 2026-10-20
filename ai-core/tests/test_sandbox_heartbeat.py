"""긴 샌드박스 호출 동안 progress heartbeat가 뛰는지 검증한다.

layer-2(카오스)는 clone/build/배포/장애주입/복구가 샌드박스 안에서 수 분 걸린다.
그 동안 progress 이벤트가 하나도 안 나가면 서버 stale 스위퍼가 "갱신 없음"으로
오인해 멀쩡히 진행 중인 작업을 ERROR로 확정한다. heartbeat는 이를 막는다.
"""
import time
import unittest

from app.workflow.repository_validation import _sandbox_heartbeat


class SandboxHeartbeatTests(unittest.TestCase):
    def test_emits_periodically_during_block_then_stops(self):
        calls = []

        def emit(step, **kwargs):
            calls.append((step, kwargs.get("detail")))

        with _sandbox_heartbeat(emit, "BASELINE", interval=0.05):
            time.sleep(0.17)  # ~3 intervals
        during = len(calls)
        self.assertGreaterEqual(during, 2, "heartbeat가 블록 동안 주기적으로 안 뜀")
        self.assertTrue(all(step == "BASELINE" for step, _ in calls))

        time.sleep(0.12)
        self.assertEqual(len(calls), during, "컨텍스트 종료 후에도 heartbeat가 계속 뜀")

    def test_heartbeat_failure_never_propagates(self):
        def boom(step, **kwargs):
            raise RuntimeError("publish down")

        # 예외가 밖으로 새면 heartbeat가 본 실행을 깨뜨린다는 뜻이다. 그러면 테스트 실패.
        with _sandbox_heartbeat(boom, "BASELINE", interval=0.02):
            time.sleep(0.07)


if __name__ == "__main__":
    unittest.main()
