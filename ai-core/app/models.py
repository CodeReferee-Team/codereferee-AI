from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class RepositoryValidationRequest(BaseModel):
    repository_url: HttpUrl
    branch: str | None = None
    commit_sha: str | None = None
    request_id: str | None = None
    max_retries: int | None = Field(default=None, ge=0, le=10)


class CreateValidationResponse(BaseModel):
    job_id: str
    status: JobStatus


class SandboxResult(BaseModel):
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    server_started: bool = False
    server_url: str | None = None
    http_status: int | None = None
    browser_loaded: bool = False
    page_title: str | None = None
    run_command: list[str] | None = None
    service_check_attempted: bool = Field(default=False, exclude=True)
    browser_check_attempted: bool = Field(default=False, exclude=True)

    @property
    def log(self) -> str:
        parts = [
            f"exit_code={self.exit_code}",
            f"timed_out={self.timed_out}",
            f"duration_ms={self.duration_ms}",
            f"server_started={self.server_started}",
            f"server_url={self.server_url}",
            f"http_status={self.http_status}",
            f"browser_loaded={self.browser_loaded}",
            f"page_title={self.page_title}",
            "stdout:",
            self.stdout.strip(),
            "stderr:",
            self.stderr.strip(),
        ]
        return "\n".join(parts)


class RepositoryPreflightReport(BaseModel):
    repository_url: str
    cloneable: bool = False
    executable: bool = False
    resolved_commit_sha: str | None = None
    detected_stack: str | None = None
    build_command: str | None = None
    test_command: str | None = None
    run_command: str | None = None
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class AgentState(BaseModel):
    job_id: str
    request_id: str | None = None
    repository_url: str
    branch: str | None = None
    requested_commit_sha: str | None = None
    resolved_commit_sha: str | None = None
    validation_plan: dict[str, Any] = Field(default_factory=dict)
    preflight_report: RepositoryPreflightReport | None = None
    execution_result: SandboxResult | None = None
    judge_report: dict[str, Any] = Field(default_factory=dict)
    critic_feedback: dict[str, Any] = Field(default_factory=dict)
    refiner_report: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_count: int = 0
    status: JobStatus = JobStatus.queued
    events: list[str] = Field(default_factory=list)


class RepositoryValidationResponse(BaseModel):
    request_id: str | None = None
    job_id: str
    status: JobStatus
    repository_url: str
    branch: str | None = None
    commit_sha: str | None = None
    validation_plan: dict[str, Any]
    preflight_report: RepositoryPreflightReport | None
    execution_result: SandboxResult | None
    judge_report: dict[str, Any]
    critic_feedback: dict[str, Any]
    refiner_report: dict[str, Any]
    metrics: dict[str, Any]
    events: list[str]


class JobResponse(RepositoryValidationResponse):
    error_count: int
