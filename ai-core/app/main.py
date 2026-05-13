from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.models import (
    AgentCriticRequest,
    AgentCriticResponse,
    AgentState,
    CreateJobRequest,
    CreateJobResponse,
    JobResponse,
    JobStatus,
)
from app.storage.memory import job_store
from app.workflow.agent_critic import run_agent_critic_pipeline
from app.workflow.pipeline import run_pipeline

app = FastAPI(title="CodeReferee AI Core", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", response_model=CreateJobResponse)
def create_job(request: CreateJobRequest, background_tasks: BackgroundTasks) -> CreateJobResponse:
    job_id = str(uuid4())
    state = AgentState(job_id=job_id, requirement=request.requirement, status=JobStatus.queued)
    job_store.save(state)
    background_tasks.add_task(run_pipeline, state, request.max_retries)
    return CreateJobResponse(job_id=job_id, status=JobStatus.queued)


@app.post("/pipelines/agent-critic", response_model=AgentCriticResponse)
def run_agent_critic(request: AgentCriticRequest) -> AgentCriticResponse:
    return run_agent_critic_pipeline(request)


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    state = job_store.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="job not found")
    return JobResponse(**state.model_dump())


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
