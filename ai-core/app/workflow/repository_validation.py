from uuid import uuid4

from prometheus_client import Counter, Histogram

from app.agents.nodes import critic_node, judge_node, planner_node, refiner_node
from app.models import (
    AgentState,
    JobStatus,
    RepositoryValidationRequest,
    RepositoryValidationResponse,
)
from app.queue.redis_queue import redis_task_queue
from app.repository.preflight import repository_preflight_runner
from app.sandbox.docker_runner import sandbox_runner
from app.storage.memory import job_store

VALIDATION_COUNTER = Counter("codereferee_repository_validations_total", "Total repository validations", ["status"])
SANDBOX_DURATION = Histogram("codereferee_repository_sandbox_duration_ms", "Repository sandbox duration in ms")
REPOSITORY_VALIDATION_TASK = "repository_validation"


def create_validation_state(request: RepositoryValidationRequest, job_id: str | None = None) -> AgentState:
    return AgentState(
        job_id=job_id or str(uuid4()),
        request_id=request.request_id,
        repository_url=str(request.repository_url),
        branch=request.branch,
        requested_commit_sha=request.commit_sha,
        status=JobStatus.queued,
    )


def enqueue_repository_validation(request: RepositoryValidationRequest, queue=redis_task_queue) -> AgentState:
    """Persist a queued validation job and push its payload to Redis."""
    state = create_validation_state(request)
    state.events.append("Input: GitHub repository URL received")
    state.events.append("Queue: repository validation enqueued")
    job_store.save(state)
    queue.enqueue(_queue_payload(state))
    return state


def process_next_repository_validation(
    queue=redis_task_queue, *, block: bool = False, timeout: int = 0
) -> AgentState | None:
    """Process one repository-validation task from Redis.

    block=True maps to Redis BLPOP and is intended for worker.py.
    block=False maps to LPOP and is intended for non-blocking HTTP/admin checks.
    """
    payload = queue.dequeue(block=block, timeout=timeout)
    if payload is None:
        return None
    state = _state_from_queue_payload(payload)
    state.events.append("Queue: repository validation dequeued")
    return execute_repository_validation(state)


def run_repository_validation(request: RepositoryValidationRequest, job_id: str | None = None) -> AgentState:
    """Run validation synchronously, bypassing Redis. Useful for local smoke tests."""
    state = create_validation_state(request, job_id)
    state.events.append("Input: GitHub repository URL received")
    return execute_repository_validation(state)


def execute_repository_validation(state: AgentState) -> AgentState:
    state.status = JobStatus.running
    state.events.append("Workflow: repository validation started")
    job_store.save(state)

    state.preflight_report = repository_preflight_runner.run(
        state.repository_url,
        branch=state.branch,
        commit_sha=state.requested_commit_sha,
    )
    state.resolved_commit_sha = state.preflight_report.resolved_commit_sha
    state.events.append("Preflight: repository accessibility checked")
    preflight_passed = _preflight_passed(state.preflight_report)
    state.events.append(f"Preflight: {'passed' if preflight_passed else 'failed'}")
    state = planner_node(state)

    if preflight_passed:
        state.events.append("Sandbox: repository clone and smoke validation started")
        state.execution_result = sandbox_runner.run_repository(
            state.preflight_report.repository_url,
            branch=state.branch,
            commit_sha=state.requested_commit_sha,
        )
        state.metrics = _metrics_from_execution(state)
        SANDBOX_DURATION.observe(state.execution_result.duration_ms)
        state.events.append("Sandbox: repository clone and smoke validation finished")
    else:
        state.events.append("Sandbox: skipped because preflight failed")
        state.metrics = _metrics_from_execution(state)

    state = judge_node(state)
    state = critic_node(state)
    state = refiner_node(state)
    VALIDATION_COUNTER.labels(status=state.status).inc()
    job_store.save(state)
    return state


def to_response(state: AgentState, request_id: str | None = None) -> RepositoryValidationResponse:
    return RepositoryValidationResponse(
        request_id=request_id or state.request_id,
        job_id=state.job_id,
        status=state.status,
        repository_url=state.repository_url,
        branch=state.branch,
        commit_sha=state.resolved_commit_sha or state.requested_commit_sha,
        validation_plan=state.validation_plan,
        preflight_report=state.preflight_report,
        execution_result=state.execution_result,
        judge_report=state.judge_report,
        critic_feedback=state.critic_feedback,
        refiner_report=state.refiner_report,
        metrics=state.metrics,
        events=state.events,
    )


def _queue_payload(state: AgentState) -> dict[str, str | None]:
    # Match the server Redis schema so POST /jobs and server-originated tasks are interchangeable.
    return {
        "taskId": state.job_id,
        "repositoryUrl": state.repository_url,
        "branch": state.branch,
        "commitSha": state.requested_commit_sha,
        "submittedAt": None,
    }


def _state_from_queue_payload(payload: dict) -> AgentState:
    if "repositoryUrl" in payload or "taskId" in payload:
        job_id = payload.get("taskId")
        repository_url = payload.get("repositoryUrl")
        branch = payload.get("branch")
        commit_sha = payload.get("commitSha")
        request_id = payload.get("taskId")
        source = "server"
    elif payload.get("type") == REPOSITORY_VALIDATION_TASK:
        job_id = payload.get("job_id")
        repository_url = payload.get("repository_url")
        branch = payload.get("branch")
        commit_sha = payload.get("commit_sha")
        request_id = payload.get("request_id") or payload.get("job_id")
        source = "ai"
    else:
        raise ValueError(f"Unsupported queue task schema: {sorted(payload.keys())}")

    if not job_id or not repository_url:
        raise ValueError("Repository validation task requires taskId/job_id and repositoryUrl/repository_url")

    request = RepositoryValidationRequest(
        repository_url=repository_url,
        branch=branch,
        commit_sha=commit_sha,
        request_id=request_id,
    )
    state = create_validation_state(request, job_id=job_id)
    if submitted_at := payload.get("submittedAt"):
        state.events.append(f"Queue: submittedAt={submitted_at}")
    state.events.append(f"Queue: payload schema={source}")
    return state


def _preflight_passed(report) -> bool:
    return bool(report and report.cloneable and report.executable)


def _metrics_from_execution(state: AgentState) -> dict[str, object]:
    preflight_passed = _preflight_passed(state.preflight_report)
    result = state.execution_result
    if result is None:
        return {
            "cloneable": bool(state.preflight_report and state.preflight_report.cloneable),
            "preflight_passed": preflight_passed,
            "sandbox_executed": False,
        }
    return {
        "cloneable": bool(state.preflight_report and state.preflight_report.cloneable),
        "preflight_passed": preflight_passed,
        "sandbox_executed": True,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "server_started": result.server_started,
        "server_url": result.server_url,
        "http_status": result.http_status,
        "browser_loaded": result.browser_loaded,
        "page_title": result.page_title,
        "service_check_attempted": result.service_check_attempted,
        "browser_check_attempted": result.browser_check_attempted,
    }
