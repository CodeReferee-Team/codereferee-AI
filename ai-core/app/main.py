from fastapi import FastAPI, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.models import CreateValidationResponse, JobResponse, RepositoryValidationRequest, RepositoryValidationResponse
from app.storage.memory import job_store
from app.workflow.repository_validation import (
    enqueue_repository_validation,
    process_next_repository_validation,
    run_repository_validation,
    to_response,
)

app = FastAPI(title="CodeReferee AI Core", version="0.2.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/validations/repository", response_model=RepositoryValidationResponse)
def validate_repository(request: RepositoryValidationRequest) -> RepositoryValidationResponse:
    state = run_repository_validation(request)
    return to_response(state)


@app.post("/v1/validations/repository", response_model=RepositoryValidationResponse)
def validate_repository_v1(request: RepositoryValidationRequest) -> RepositoryValidationResponse:
    state = run_repository_validation(request)
    return to_response(state)


@app.post("/jobs", response_model=CreateValidationResponse)
def create_validation_job(request: RepositoryValidationRequest) -> CreateValidationResponse:
    state = enqueue_repository_validation(request)
    return CreateValidationResponse(job_id=state.job_id, status=state.status)


@app.post("/workers/repository/next", response_model=None)
def process_next_repository_job():
    state = process_next_repository_validation()
    if state is None:
        return Response(status_code=204)
    return to_response(state)


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    state = job_store.get(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="job not found")
    response = to_response(state)
    return JobResponse(**response.model_dump(), error_count=state.error_count)


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
