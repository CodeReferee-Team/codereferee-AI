from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    # CodeReferee 인프라 문제로 판정 불가. 사용자 레포 결함(failed)과 절대 섞지 않는다.
    error = "error"


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
    infra_error: str | None = None
    service_check_attempted: bool = Field(default=False, exclude=True)
    browser_check_attempted: bool = Field(default=False, exclude=True)
    schema_version: str | None = None
    probe_transport: str | None = None
    baseline: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    chaos_observation: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    sandbox_report: dict[str, Any] = Field(default_factory=dict)

    @property
    def sandbox_summary(self) -> str:
        """구조화 결과의 한 줄 요약.

        baseline/metrics/chaos_observation을 dict 그대로 로그에 붙이면
        실제 stdout/stderr가 밀려나고 같은 내용이 evidence마다 중복된다.
        원본은 각 필드에 그대로 남아 있으므로 여기서는 요약만 만든다.
        """
        report = self.sandbox_report
        if not report:
            return ""
        parts = [f"stack={report.get('detected_stack')}", f"outcome={report.get('outcome')}"]
        failed_step = report.get("failed_step")
        if failed_step and failed_step != "none":
            parts.append(f"failed_step={failed_step}")
        steps = report.get("steps") or []
        if isinstance(steps, list) and steps:
            rendered = ",".join(
                f"{step.get('name')}:{step.get('exit_code')}({step.get('duration_ms')}ms)"
                for step in steps
                if isinstance(step, dict)
            )
            parts.append(f"steps={rendered}")
        return " ".join(parts)

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
        ]
        if self.schema_version:
            parts.append(f"schema_version={self.schema_version}")
        if self.probe_transport:
            parts.append(f"probe_transport={self.probe_transport}")
        if self.sandbox_summary:
            parts.append(f"sandbox={self.sandbox_summary}")
        parts += ["stdout:", self.stdout.strip(), "stderr:", self.stderr.strip()]
        return "\n".join(parts)


class RepositoryPreflightReport(BaseModel):
    repository_url: str
    infra_error: str | None = None
    cloneable: bool = False
    executable: bool = False
    resolved_commit_sha: str | None = None
    detected_stack: str | None = None
    build_command: str | None = None
    test_command: str | None = None
    run_command: str | None = None
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)


class SLI(BaseModel):
    availability_percent: float | None = None
    p95_latency_ms: float | None = None
    p99_latency_ms: float | None = None
    error_rate: float | None = None
    throughput_rps: float | None = None


class SLO(BaseModel):
    availability_percent_min: float | None = None
    p95_latency_ms_max: float | None = None
    p99_latency_ms_max: float | None = None
    error_rate_max: float | None = None
    throughput_rps_min: float | None = None


# 기본 SLO 목표값. 관측값이 아니라 설정이며 운영자가 바꾸는 것을 전제로 한다.
# docs/judge-policy.md 6.4 참고: 이 값과 정책 문서 3절 표가 서로 달라 통합이 필요하다.
DEFAULT_SLO = SLO(
    availability_percent_min=99.9,
    p95_latency_ms_max=30000.0,
    p99_latency_ms_max=60000.0,
    error_rate_max=0.01,
    throughput_rps_min=0.01,
)


class ErrorBudget(BaseModel):
    allowed_error_rate: float | None = None
    observed_error_rate: float | None = None
    budget_remaining_percent: float | None = None


class ChaosObservation(BaseModel):
    scenario: str | None = None
    target: str | None = None
    duration_sec: int | None = None
    recovered: bool | None = None
    recovery_time_sec: float | None = None


class SREMetrics(BaseModel):
    sli: SLI = Field(default_factory=SLI)
    slo: SLO = Field(default_factory=SLO)
    error_budget: ErrorBudget = Field(default_factory=ErrorBudget)
    chaos: ChaosObservation = Field(default_factory=ChaosObservation)


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
    sre_metrics: SREMetrics = Field(default_factory=SREMetrics)
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
    sre_metrics: SREMetrics = Field(default_factory=SREMetrics)
    events: list[str]


class JobResponse(RepositoryValidationResponse):
    error_count: int
