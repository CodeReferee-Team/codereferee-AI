from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class StrictAgentReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PlannerReport(StrictAgentReport):
    objective: str = Field(min_length=1)
    validation_scope: list[str] = Field(min_length=1)
    chaos_scenarios: list[str] = Field(min_length=1)
    metrics_required: list[str] = Field(min_length=1)
    stop_conditions: list[str] = Field(min_length=1)

    @field_validator("objective")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("validation_scope", "chaos_scenarios", "metrics_required", "stop_conditions")
    @classmethod
    def _non_blank_items(cls, value: list[str]) -> list[str]:
        return _require_non_blank_items(value)


# 판정 사유의 정규 코드. docs/judge-policy.md 3절과 6절 기준표에서 왔다.
# 자유 문자열로 두면 같은 원인이 매번 다르게 적혀 분류 정확도를 잴 수 없다.
REASON_CATEGORIES = (
    # preflight
    "repository_not_found",
    "ref_not_found",
    "private_repository_not_supported",
    "repository_not_accessible",
    "invalid_repository_input",
    "no_manifest_detected",
    "ambiguous_monorepo_path",
    "empty_repository",
    "unsupported_project_stack",
    # sandbox
    "timeout",
    "sandbox_nonzero_exit",
    "sandbox_not_executed",
    "test_failure",
    "dependency_install_failed",
    "docker_build_failed",
    # runtime
    "service_smoke_failed",
    "browser_smoke_failed",
    # metrics
    "latency_slo_violation",
    "error_rate_slo_violation",
    "availability_slo_violation",
    "cpu_saturation",
    "memory_pressure",
    "unexpected_restart",
    "database_connection_errors",
    "redis_connection_errors",
    "no_traffic_observed",
    "missing_metrics",
    # chaos
    "chaos_not_recovered",
    "chaos_error_budget_exhausted",
    "chaos_recovery_exceeds_expected_bound",
    "chaos_recovered_within_budget",
    # pass
    "all_checks_passed",
)


class JudgeReport(StrictAgentReport):
    status: Literal["Pass", "Fail"]
    reason_category: Literal[REASON_CATEGORIES]  # type: ignore[valid-type]
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("evidence")
    @classmethod
    def _non_blank_items(cls, value: list[str]) -> list[str]:
        return _require_non_blank_items(value)


class CriticReport(StrictAgentReport):
    issue: str = Field(min_length=1)
    root_cause: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    recommended_action: str = Field(min_length=1)

    @field_validator("issue", "root_cause", "recommended_action")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("evidence")
    @classmethod
    def _non_blank_items(cls, value: list[str]) -> list[str]:
        return _require_non_blank_items(value)


class RefinerEdit(StrictAgentReport):
    """설정/매니페스트 한 곳을 고치는 지시. LLM은 '무엇을 바꿀지'만 고르고,
    git apply가 먹는 unified diff는 노드가 원본 파일에서 결정적으로 만든다.
    find는 파일에 그대로 존재하는 부분 문자열이어야 한다(들여쓰기 포함)."""

    path: str = Field(min_length=1)
    find: str = Field(min_length=1)
    replace: str

    @field_validator("path", "find")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        return _require_non_blank(value)


class RefinerReport(StrictAgentReport):
    summary: str = Field(min_length=1)
    patch_guidance: list[str] = Field(min_length=1)
    verification_steps: list[str] = Field(min_length=1)
    risk: Literal["low", "medium", "high"]
    # repository_files에 내용이 실린 설정/매니페스트 파일에 대한 구체적 수정 지시.
    # 노드가 이걸 원본과 대조해 patch_diff로 렌더한다. 고칠 파일 내용이 없으면 빈 리스트.
    edits: list[RefinerEdit] = Field(default_factory=list)
    # base commit 대비 누적 unified diff. 보통 edits에서 노드가 채운다. LLM 없이 도는
    # 결정적 fallback은 diff를 만들 수 없어 None이다. 없으면 재검증 루프가 돌지 않는다.
    patch_diff: str | None = None

    @field_validator("summary")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("patch_guidance", "verification_steps")
    @classmethod
    def _non_blank_items(cls, value: list[str]) -> list[str]:
        return _require_non_blank_items(value)


ReportT = TypeVar("ReportT", bound=StrictAgentReport)


def validate_report(schema: type[ReportT], value: dict[str, Any]) -> dict[str, Any]:
    return schema.model_validate(value).model_dump(mode="json")


def validate_or_fallback(
    schema: type[ReportT], value: dict[str, Any], fallback: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    events: list[str] = []
    try:
        return validate_report(schema, value), events
    except ValidationError as exc:
        events.append(f"Agent schema rejected output: {_validation_summary(exc)}")
        return validate_report(schema, fallback), events


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _require_non_blank_items(value: list[str]) -> list[str]:
    if not value:
        raise ValueError("must not be empty")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("items must be non-blank strings")
    return value


def _validation_summary(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "unknown validation error"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", [])) or "report"
    return f"{loc}: {first.get('msg', 'invalid')}"
