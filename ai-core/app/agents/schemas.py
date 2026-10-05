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
    # 테스트가 없어 검증 자체를 못 한 경우. 코드 결함과 구분해야 집계가 의미를 갖는다.
    "no_tests_detected",
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


class SourceEdit(StrictAgentReport):
    """내용으로 앵커하는 편집. 줄 번호를 쓰지 않아 off-by-one이 생길 수 없다."""

    path: str = Field(min_length=1)
    # 바꿀 원본 줄. 파일에 정확히 한 번 나타나야 한다.
    find: list[str] = Field(min_length=1)
    # 그 자리에 넣을 줄. 비우면 삭제다.
    replace: list[str]


class RefinerReport(StrictAgentReport):
    summary: str = Field(min_length=1)
    patch_guidance: list[str] = Field(min_length=1)
    verification_steps: list[str] = Field(min_length=1)
    risk: Literal["low", "medium", "high"]
    # base commit 대비 누적 unified diff. LLM 없이 도는 결정적 fallback은
    # diff를 만들 수 없으므로 선택 필드다. 없으면 재검증 루프가 돌지 않는다.
    # 고칠 부분만 담는 편집 목록. 모델은 diff도 파일 전문도 쓰지 않는다.
    # diff는 형식(context 줄, hunk 헤더)을 못 맞추고, 전문은 끝까지 쓰지 못해 멀쩡한 코드가
    # 지워졌다. docs/evaluation-design.md 6절.
    edits: list[SourceEdit] | None = None
    # 위 편집에서 우리가 difflib으로 만든다. 모델 출력이 아니다.
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
