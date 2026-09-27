from typing import Any

from pydantic import ValidationError

from app.agents.evidence import build_evidence_packet, classify_failure_category, render_evidence_packet
from app.agents.llm import llm, parse_json_strict
from app.agents.prompts import CRITIC_PROMPT, JUDGE_PROMPT, PLANNER_PROMPT, REFINER_PROMPT
from app.agents.schemas import CriticReport, JudgeReport, PlannerReport, RefinerReport, StrictAgentReport, validate_report
from app.models import DEFAULT_SLO, AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


# 카오스 구간 p95가 baseline의 몇 배를 넘으면 경고할지. 출처 있는 값이 아니라 우리 관례다
# (docs/judge-policy.md 6.3). Sandbox가 p99를 보내기 시작하면 임계값을 다시 정한다.
LATENCY_DEGRADATION_FACTOR = 10


def planner_node(state: AgentState) -> AgentState:
    state.events.append("Planner: repository validation plan prepared")
    packet = build_evidence_packet(state)
    fallback = _fallback_plan(state)
    if llm.enabled:
        state.validation_plan = _invoke_validated_report(
            role="Planner",
            schema=PlannerReport,
            system_prompt=PLANNER_PROMPT,
            user_prompt="Repository: {repository_url}\nEvidence packet: {evidence}",
            values={
                "repository_url": state.repository_url,
                "evidence": render_evidence_packet(packet),
            },
            fallback=fallback,
            events=state.events,
        )
    else:
        state.validation_plan = validate_report(PlannerReport, fallback)
    return state


def judge_node(state: AgentState) -> AgentState:
    state.events.append("Judge: repository validation result evaluated")
    packet = build_evidence_packet(state)
    fallback = _fallback_judge(state)
    if llm.enabled:
        report = _invoke_validated_report(
            role="Judge",
            schema=JudgeReport,
            system_prompt=JUDGE_PROMPT,
            user_prompt="Evidence packet:\n{evidence}",
            values={
                "evidence": render_evidence_packet(packet),
            },
            fallback=fallback,
            events=state.events,
        )
    else:
        report = validate_report(JudgeReport, fallback)

    state.judge_report = report
    if str(report.get("status", "")).lower() == "pass":
        state.status = JobStatus.success
    else:
        state.status = JobStatus.failed
        state.error_count += 1
    return state


def critic_node(state: AgentState) -> AgentState:
    state.events.append("Critic: repository reliability gap analyzed")
    packet = build_evidence_packet(state)
    fallback = _fallback_critic(state)
    if llm.enabled:
        state.critic_feedback = _invoke_validated_report(
            role="Critic",
            schema=CriticReport,
            system_prompt=CRITIC_PROMPT,
            user_prompt="Evidence packet:\n{evidence}",
            values={
                "evidence": render_evidence_packet(packet),
            },
            fallback=fallback,
            events=state.events,
        )
    else:
        state.critic_feedback = validate_report(CriticReport, fallback)
    return state


def refiner_node(state: AgentState) -> AgentState:
    state.events.append("Refiner: remediation guidance prepared")
    packet = build_evidence_packet(state)
    fallback = _fallback_refiner(state)
    if llm.enabled:
        state.refiner_report = _invoke_validated_report(
            role="Refiner",
            schema=RefinerReport,
            system_prompt=REFINER_PROMPT,
            user_prompt="Repository: {repository_url}\nEvidence packet:\n{evidence}",
            values={
                "repository_url": state.repository_url,
                "evidence": render_evidence_packet(packet),
            },
            fallback=fallback,
            events=state.events,
        )
    else:
        state.refiner_report = validate_report(RefinerReport, fallback)
    return state


def _fallback_plan(state: AgentState) -> dict[str, object]:
    category = classify_failure_category(state)
    return {
        "objective": "Validate an existing GitHub repository instead of generating new code.",
        "validation_scope": ["cloneability", "project type and manifest detection", "build/test/run smoke", "service HTTP/browser smoke when attempted", f"failure_category={category}"],
        "chaos_scenarios": ["bounded execution timeout", "resource limits", "log/error inspection"],
        "metrics_required": ["exit_code", "duration_ms", "timed_out", "stdout/stderr evidence", "http_status and browser_loaded when applicable"],
        "stop_conditions": ["uncloneable repository", "no supported manifest or executable entrypoint", "sandbox timeout", "non-zero execution", "failed service or browser smoke check"],
    }


def _invoke_validated_report(
    *,
    role: str,
    schema: type[StrictAgentReport],
    system_prompt: str,
    user_prompt: str,
    values: dict[str, Any],
    fallback: dict[str, Any],
    events: list[str],
) -> dict[str, Any]:
    raw = llm.invoke_text(system_prompt, user_prompt, values)
    try:
        return validate_report(schema, parse_json_strict(raw))
    except (ValueError, ValidationError) as exc:
        events.append(f"{role}: Agent schema rejected output: {_event_error(exc)}")
        try:
            repaired = llm.invoke_schema_repair(
                schema_name=schema.__name__,
                schema_json=schema.model_json_schema(),
                original_response=raw,
                validation_error=_event_error(exc),
            )
            report = validate_report(schema, parse_json_strict(repaired))
            events.append(f"{role}: Agent schema repair accepted")
            return report
        except (ValueError, ValidationError) as repair_exc:
            events.append(f"{role}: Agent schema repair failed: {_event_error(repair_exc)}")
            events.append(f"{role}: Deterministic fallback selected")
            return validate_report(schema, fallback)


def _fallback_judge(state: AgentState) -> dict[str, object]:
    preflight = state.preflight_report
    if preflight is None:
        return {"status": "Fail", "reason_category": "sandbox_not_executed", "reason": "No preflight report was produced.", "evidence": ["preflight_report=missing"]}
    if not preflight.cloneable:
        return {"status": "Fail", "reason_category": _preflight_category(preflight), "reason": preflight.reason or "Repository cannot be cloned.", "evidence": _non_empty_evidence(preflight.evidence, preflight.reason, "cloneable=false")}
    if not preflight.executable:
        return {"status": "Fail", "reason_category": _no_entrypoint_category(preflight), "reason": preflight.reason or "Repository has no detected executable path.", "evidence": _non_empty_evidence(preflight.evidence, preflight.reason, "executable=false")}

    result = state.execution_result or SandboxResult(exit_code=None, stderr="No sandbox execution")
    if result.timed_out:
        return {"status": "Fail", "reason_category": "timeout", "reason": "Sandbox execution timed out.", "evidence": [result.log]}
    if result.exit_code != 0:
        return {"status": "Fail", "reason_category": _nonzero_exit_category(result), "reason": result.stderr.strip() or "Sandbox returned non-zero exit code.", "evidence": [result.log]}
    if result.service_check_attempted and not _service_smoke_passed(result):
        return {"status": "Fail", "reason_category": "service_smoke_failed", "reason": "Service smoke check failed after sandbox execution.", "evidence": [result.log]}
    if result.browser_check_attempted and not result.browser_loaded:
        return {"status": "Fail", "reason_category": "browser_smoke_failed", "reason": "Browser smoke check failed after service startup.", "evidence": [result.log]}

    failure, warnings = _measured_policy_findings(state, result)
    state.metrics["policy_warnings"] = warnings
    for warning in warnings:
        state.events.append(f"Judge: warning {warning}")
    if failure:
        category, _, detail = failure.partition(": ")
        return {"status": "Fail", "reason_category": category, "reason": detail or failure, "evidence": [result.log]}
    passed_category = "chaos_recovered_within_budget" if result.chaos_observation else "all_checks_passed"
    return {
        "status": "Pass",
        "reason_category": passed_category,
        "reason": "Repository passed preflight and sandbox smoke validation.",
        "evidence": [result.log],
    }


def _measured_policy_findings(state: AgentState, result: SandboxResult) -> tuple[str | None, list[str]]:
    """docs/judge-policy.md 6절 기준으로 실측 지표를 판정한다.

    실측값이 없으면 아무것도 판정하지 않는다. duration_ms를 p95 대용으로 쓰는 추정값에
    SLO를 걸면 느린 빌드가 전부 Fail이 되기 때문이다.
    chaos_evidence_missing과 chaos_experiment_aborted는 Error 상태가 필요해 아직 다루지 않는다.
    """
    measured = result.metrics
    if not measured:
        return None, []

    warnings: list[str] = []
    slo = state.sre_metrics.slo
    if slo.availability_percent_min is None and slo.error_rate_max is None:
        slo = DEFAULT_SLO  # 워크플로를 거치지 않고 judge만 호출한 경우
    observation = result.chaos_observation

    if observation:
        if observation.get("recovered") is False:
            return "chaos_not_recovered: chaos experiment never recovered to a serving state.", warnings

        recovery = _as_float(measured.get("recovery_seconds"))
        allowance = _monthly_unavailability_budget_seconds(slo.availability_percent_min)
        if recovery is not None and allowance:
            if recovery >= allowance:
                return (
                    f"chaos_error_budget_exhausted: recovery {recovery}s consumed the monthly "
                    f"error budget of {allowance}s.",
                    warnings,
                )
            if recovery >= allowance * 0.2:
                warnings.append("chaos_error_budget_significant_burn")

        baseline_p95 = _as_float((result.baseline.get("metrics") or {}).get("p95_latency_ms"))
        observed_p95 = _as_float(measured.get("p95_latency_ms"))
        if baseline_p95 and observed_p95 and observed_p95 > baseline_p95 * LATENCY_DEGRADATION_FACTOR:
            warnings.append("chaos_latency_degraded")
        return None, warnings

    # 카오스 실험이 아닌 실측 구간에는 docs/judge-policy.md 3절 기준표를 그대로 적용한다.
    if missing := _missing_required_metrics(measured):
        return f"missing_metrics: required metrics are absent: {', '.join(missing)}.", warnings

    for category, key, limit, over in (
        ("error_rate_slo_violation", "error_rate", slo.error_rate_max, True),
        ("latency_slo_violation", "p95_latency_ms", slo.p95_latency_ms_max, True),
        ("cpu_saturation", "cpu_usage_percent", slo.cpu_usage_percent_max, True),
        ("unexpected_restart", "restart_count", slo.restart_count_max, True),
        ("database_connection_errors", "db_connection_errors", slo.db_connection_errors_max, True),
        ("redis_connection_errors", "redis_connection_errors", slo.redis_connection_errors_max, True),
        ("no_traffic_observed", "request_count", slo.request_count_min, False),
    ):
        observed = _as_float(measured.get(key))
        if observed is None or limit is None:
            continue
        if (observed > limit) if over else (observed < limit):
            comparison = "exceeds" if over else "is below"
            return f"{category}: {key} {observed} {comparison} {limit}.", warnings

    availability = _as_float(measured.get("availability"))
    if (
        availability is not None
        and slo.availability_percent_min is not None
        and availability * 100 < slo.availability_percent_min
    ):
        return (
            f"availability_slo_violation: availability {availability * 100}% is below "
            f"{slo.availability_percent_min}%.",
            warnings,
        )

    memory_ratio = _memory_usage_ratio(measured)
    if (
        memory_ratio is not None
        and slo.memory_usage_ratio_max is not None
        and memory_ratio > slo.memory_usage_ratio_max
    ):
        return (
            f"memory_pressure: memory usage ratio {round(memory_ratio, 3)} exceeds "
            f"{slo.memory_usage_ratio_max}.",
            warnings,
        )

    return None, warnings


# SLO를 걸어둔 지표가 값 없이 비어 있으면 판정 근거가 없는 것이다. 통과로 넘기지 않는다.
REQUIRED_METRIC_KEYS = ("p95_latency_ms", "error_rate")


def _missing_required_metrics(measured: dict[str, object]) -> list[str]:
    return [key for key in REQUIRED_METRIC_KEYS if key in measured and _as_float(measured.get(key)) is None]


def _memory_usage_ratio(measured: dict[str, object]) -> float | None:
    used = _as_float(measured.get("memory_usage_mb"))
    limit = _as_float(measured.get("memory_limit_mb"))
    if used is None or not limit:
        return None
    return used / limit


def _monthly_unavailability_budget_seconds(availability_percent_min: float | None) -> float | None:
    """가용성 목표에서 월간 허용 불가용 시간을 구한다. 99.9%면 2,592초.

    출처: https://sre.google/sre-book/availability-table/
    """
    if availability_percent_min is None:
        return None
    return round((1 - availability_percent_min / 100) * 30 * 24 * 3600, 3)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _preflight_category(preflight: RepositoryPreflightReport) -> str:
    """preflight 사유 문장에서 정규 코드를 고른다. 문장은 preflight.py가 4종만 낸다."""
    text = f"{preflight.reason} {' '.join(preflight.evidence)}".lower()
    if "authentication" in text or "private" in text:
        return "private_repository_not_supported"
    if "branch" in text or "ref" in text:
        return "ref_not_found"
    if "only public github" in text or "url" in text:
        return "invalid_repository_input"
    if "not reachable" in text or "not found" in text:
        return "repository_not_found"
    return "repository_not_accessible"


def _no_entrypoint_category(preflight: RepositoryPreflightReport) -> str:
    text = f"{preflight.reason} {' '.join(preflight.evidence)}".lower()
    if "no executable files" in text or "empty" in text:
        return "empty_repository"
    if "path" in text or "monorepo" in text:
        return "ambiguous_monorepo_path"
    if "manifest" in text:
        return "no_manifest_detected"
    return "unsupported_project_stack"


def _nonzero_exit_category(result: SandboxResult) -> str:
    text = f"{result.stderr} {result.stdout}".lower()
    if "docker build" in text or "dockerfile" in text:
        return "docker_build_failed"
    if "install" in text or "npm err" in text or "pip" in text:
        return "dependency_install_failed"
    if "test" in text or "pytest" in text or "assert" in text:
        return "test_failure"
    return "sandbox_nonzero_exit"


def _fallback_critic(state: AgentState) -> dict[str, object]:
    preflight = state.preflight_report
    if preflight and not preflight.cloneable:
        return {
            "issue": "Repository intake failed before sandbox execution.",
            "root_cause": preflight.reason or "Repository or requested ref is not reachable.",
            "evidence": _non_empty_evidence(preflight.evidence, preflight.reason, "cloneable=false"),
            "recommended_action": "Verify the GitHub URL, branch, repository visibility, and network access.",
        }
    if preflight and not preflight.executable:
        return {
            "issue": "Repository has no supported build/test/run entrypoint.",
            "root_cause": preflight.reason or "No supported manifest or deterministic validation command was detected.",
            "evidence": _non_empty_evidence(preflight.evidence, preflight.reason, "executable=false"),
            "recommended_action": "Add a standard manifest and deterministic validation command, such as pytest, Gradle test, Maven test, or npm test.",
        }
    result = state.execution_result
    if result and result.timed_out:
        return {
            "issue": "Repository exceeded the bounded sandbox execution window.",
            "root_cause": "Sandbox execution timed out before validation completed.",
            "evidence": [result.log],
            "recommended_action": "Reduce blocking startup/test work, add timeout-safe startup behavior, and re-run validation from the same commit.",
        }
    if result and result.exit_code not in (0, None):
        return {
            "issue": "Repository command returned a non-zero sandbox exit code.",
            "root_cause": state.judge_report.get("reason", "Sandbox command failed."),
            "evidence": _non_empty_evidence(state.judge_report.get("evidence", []), result.stderr, f"exit_code={result.exit_code}"),
            "recommended_action": "Fix the failing build/test/run command surfaced in the logs and verify the command exits with exit_code=0.",
        }
    if result and result.service_check_attempted and not _service_smoke_passed(result):
        return {
            "issue": "Service smoke validation failed after the process started.",
            "root_cause": f"HTTP/browser service check failed with http_status={result.http_status} and browser_loaded={result.browser_loaded}.",
            "evidence": [result.log],
            "recommended_action": "Fix the app health endpoint or start command, then verify the service returns a successful HTTP status and browser probe loads.",
        }
    if result and result.browser_check_attempted and not result.browser_loaded:
        return {
            "issue": "Browser smoke validation failed.",
            "root_cause": "The service did not load successfully in the browser probe.",
            "evidence": [result.log],
            "recommended_action": "Fix client startup/rendering and verify the endpoint loads in a headless browser.",
        }
    return {
        "issue": "Repository failed sandbox reliability validation." if state.status == JobStatus.failed else "No critical reliability issue found.",
        "root_cause": state.judge_report.get("reason", "Unknown"),
        "evidence": _non_empty_evidence(state.judge_report.get("evidence", []), state.judge_report.get("reason", "")),
        "recommended_action": "Use the sandbox logs and metrics to add timeouts, health checks, resource bounds, or deterministic tests; re-run from the same commit.",
    }


def _fallback_refiner(state: AgentState) -> dict[str, object]:
    issue = str(state.critic_feedback.get("issue", "Repository validation completed."))
    action = str(state.critic_feedback.get("recommended_action", "Re-run repository validation from the same commit SHA."))
    root_cause = str(state.critic_feedback.get("root_cause", "No root cause recorded."))
    guidance = _refiner_guidance(issue, root_cause, action)
    return {
        "summary": issue,
        "patch_guidance": guidance,
        "verification_steps": ["Re-run repository validation from the same commit SHA.", "Confirm sandbox exit_code=0 and timed_out=False."],
        "risk": "medium" if state.status == JobStatus.failed else "low",
    }


def _service_smoke_passed(result: SandboxResult) -> bool:
    if result.http_status is not None and not (200 <= result.http_status < 400):
        return False
    return bool(result.server_started or result.server_url or result.http_status is not None)


def _non_empty_evidence(value: object, *fallbacks: object) -> list[str]:
    evidence: list[str] = []
    if isinstance(value, list):
        evidence.extend(str(item) for item in value if str(item).strip())
    elif isinstance(value, str) and value.strip():
        evidence.append(value)
    for fallback in fallbacks:
        if isinstance(fallback, str) and fallback.strip():
            evidence.append(fallback)
    return evidence or ["evidence=missing"]


def _refiner_guidance(issue: str, root_cause: str, action: str) -> list[str]:
    text = f"{issue} {root_cause} {action}".casefold()
    if "intake" in text or "clone" in text or "reachable" in text:
        return [action, "Verify the repository URL, branch/ref, visibility, and network access before re-running validation."]
    if "entrypoint" in text or "manifest" in text:
        return [action, "Add a deterministic supported manifest or validation command and commit it before re-running."]
    if "timeout" in text:
        return [action, "Make startup/tests timeout-safe and re-run validation to confirm the sandbox no longer times out."]
    if "non-zero" in text or "exit code" in text or "pytest" in text:
        return [action, "Fix the failing command from the logs and verify it exits with exit_code=0 locally and in the sandbox."]
    if "http" in text or "browser" in text or "service" in text:
        return [action, "Fix the service health endpoint/start command and verify HTTP plus browser smoke checks pass."]
    return [action]


def _event_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(part) for part in first.get("loc", [])) or "report"
            return f"{loc}: {first.get('msg', 'invalid')}"
    return str(exc) or exc.__class__.__name__
