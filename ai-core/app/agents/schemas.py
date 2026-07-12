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


class JudgeReport(StrictAgentReport):
    status: Literal["Pass", "Fail"]
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


class RefinerReport(StrictAgentReport):
    summary: str = Field(min_length=1)
    patch_guidance: list[str] = Field(min_length=1)
    verification_steps: list[str] = Field(min_length=1)
    risk: Literal["low", "medium", "high"]

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
