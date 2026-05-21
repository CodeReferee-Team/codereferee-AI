from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class CreateJobRequest(BaseModel):
    requirement: str = Field(..., min_length=5)
    max_retries: int | None = Field(default=None, ge=0, le=10)


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus


class AgentCriticRequest(BaseModel):
    requirement: str = Field(..., min_length=5)
    current_code: str | None = None
    refine: bool = True
    request_id: str | None = None


class AgentCriticResponse(BaseModel):
    request_id: str | None = None
    requirement: str
    plan: dict[str, Any]
    initial_code: str
    critic_feedback: dict[str, Any]
    refined_code: str
    events: list[str]


class SandboxResult(BaseModel):
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def log(self) -> str:
        parts = [
            f"exit_code={self.exit_code}",
            f"timed_out={self.timed_out}",
            f"duration_ms={self.duration_ms}",
            "stdout:",
            self.stdout.strip(),
            "stderr:",
            self.stderr.strip(),
        ]
        return "\n".join(parts)


class AgentState(BaseModel):
    job_id: str
    requirement: str
    plan: dict[str, Any] = Field(default_factory=dict)
    current_code: str = ""
    critic_feedback: dict[str, Any] = Field(default_factory=dict)
    execution_result: SandboxResult | None = None
    judge_report: dict[str, Any] = Field(default_factory=dict)
    error_count: int = 0
    status: JobStatus = JobStatus.queued
    events: list[str] = Field(default_factory=list)


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    requirement: str
    plan: dict[str, Any]
    current_code: str
    critic_feedback: dict[str, Any]
    judge_report: dict[str, Any]
    error_count: int
    events: list[str]
    execution_result: SandboxResult | None
