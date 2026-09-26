"""Backend가 소비하는 Redis output 이벤트를 만든다.

계약은 codereferee-server의 ResultQueueConsumer와 그 테스트에서 확정했다.
- step 값은 서버 AgentStep enum과 정확히 일치해야 한다. 모르는 step은 서버가 무시한다.
- status는 success / fail / error 중 하나다. 서버는 success -> PASSED, error 계열 -> ERROR,
  나머지 -> FAILED로 매핑한다.
- round는 1부터 세며 서버의 iterationCount가 된다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import AgentState, JobStatus

PREFLIGHT = "PREFLIGHT"
BASELINE = "BASELINE"
CHAOS = "CHAOS"
JUDGING = "JUDGING"
REFINING = "REFINING"

# AI 내부 상태를 서버가 기대하는 문자열로 옮긴다. 서버 테스트가 쓰는 값에 맞춘다.
_STATUS_WIRE_VALUES = {
    JobStatus.success: "success",
    JobStatus.failed: "fail",
    JobStatus.error: "error",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def progress_event(
    state: AgentState,
    step: str,
    *,
    round_: int | None = None,
    max_rounds: int | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "progress",
        "request_id": state.request_id,
        "step": step,
        "round": round_,
        "max_rounds": max_rounds,
        "detail": detail,
        "timestamp": _timestamp(),
    }


def result_event(state: AgentState) -> dict[str, Any]:
    return {
        "type": "result",
        "request_id": state.request_id,
        "job_id": state.job_id,
        "status": _STATUS_WIRE_VALUES.get(state.status, "fail"),
        "repository_url": state.repository_url,
        "branch": state.branch,
        "commit_sha": state.resolved_commit_sha or state.requested_commit_sha,
        "validation_plan": state.validation_plan,
        "preflight_report": state.preflight_report.model_dump() if state.preflight_report else {},
        "execution_result": state.execution_result.model_dump() if state.execution_result else {},
        "judge_report": state.judge_report,
        "critic_feedback": state.critic_feedback,
        "refiner_report": state.refiner_report,
        "metrics": state.metrics,
        "events": state.events,
    }
