from prometheus_client import Counter, Histogram

from app.agents.nodes import critic_node, draft_node, judge_node, planner_node, refiner_node
from app.config import get_settings
from app.models import AgentState, JobStatus
from app.sandbox.docker_runner import sandbox_runner
from app.storage.memory import job_store

JOB_COUNTER = Counter("codereferee_jobs_total", "Total CodeReferee jobs", ["status"])
SANDBOX_DURATION = Histogram("codereferee_sandbox_duration_ms", "Sandbox duration in ms")


def run_pipeline(state: AgentState, max_retries: int | None = None) -> AgentState:
    settings = get_settings()
    retry_limit = settings.max_self_healing_retries if max_retries is None else max_retries
    state.status = JobStatus.running
    job_store.save(state)

    state = planner_node(state)
    state = draft_node(state)

    while True:
        state = critic_node(state)
        state = refiner_node(state)
        state.events.append("Sandbox: code execution started")
        state.execution_result = sandbox_runner.run_python(state.current_code)
        SANDBOX_DURATION.observe(state.execution_result.duration_ms)
        state.events.append("Sandbox: code execution finished")
        state = judge_node(state)
        job_store.save(state)

        if state.status == JobStatus.success:
            JOB_COUNTER.labels(status="success").inc()
            return state
        if state.error_count >= retry_limit:
            state.events.append("Router: max retry limit reached")
            JOB_COUNTER.labels(status="failed").inc()
            job_store.save(state)
            return state

        state.events.append(f"Router: retrying self-healing loop ({state.error_count}/{retry_limit})")
