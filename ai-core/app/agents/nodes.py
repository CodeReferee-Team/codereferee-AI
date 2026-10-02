from typing import Any

from pydantic import ValidationError

from app.agents.evidence import build_evidence_packet, classify_failure_category, render_evidence_packet, truncate_log
from app.agents.llm import llm, parse_json_strict
from app.agents.prompts import CRITIC_PROMPT, JUDGE_PROMPT, PLANNER_PROMPT, REFINER_PROMPT
from app.agents.schemas import CriticReport, JudgeReport, PlannerReport, RefinerReport, StrictAgentReport, validate_report
from app.config import get_settings
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
        return {"status": "Fail", "reason": "No preflight report was produced.", "evidence": ["preflight_report=missing"]}
    if not preflight.cloneable:
        return {"status": "Fail", "reason": preflight.reason or "Repository cannot be cloned.", "evidence": _non_empty_evidence(preflight.evidence, preflight.reason, "cloneable=false")}
    if not preflight.executable:
        return {"status": "Fail", "reason": preflight.reason or "Repository has no detected executable path.", "evidence": _non_empty_evidence(preflight.evidence, preflight.reason, "executable=false")}

    result = state.execution_result or SandboxResult(exit_code=None, stderr="No sandbox execution")
    if is_real_chaos_observation(result):
        # 카오스 실행에는 일반 smoke 규칙을 적용하지 않는다. sandbox가 별도 서버 프로세스를 띄우지
        # 않고 Kubernetes Service probe로 관측하므로 server_started=false와 http_status=null은
        # 실패가 아니라 "그 검사를 하지 않았다"는 뜻이다. 실제 복구 성공이 "서비스 기동 실패"로
        # 판정되던 것을 막는다.
        failure, warnings = _measured_policy_findings(state, result)
        state.metrics["policy_warnings"] = warnings
        for warning in warnings:
            state.events.append(f"Judge: warning {warning}")
        if failure:
            category, _, detail = failure.partition(": ")
            return {"status": "Fail", "reason_category": category, "reason": detail or failure,
                    "evidence": _sandbox_evidence(result)}
        return {"status": "Pass", "reason_category": "chaos_recovered_within_budget",
                "reason": "Chaos experiment recovered within the configured budget.",
                "evidence": _sandbox_evidence(result)}

    if result.timed_out:
        return {"status": "Fail", "reason": "Sandbox execution timed out.", "evidence": _sandbox_evidence(result)}
    if result.exit_code != 0:
        return {"status": "Fail", "reason": _sandbox_failure_reason(result), "evidence": _sandbox_evidence(result)}
    if result.service_check_attempted and not _service_smoke_passed(result):
        return {"status": "Fail", "reason": "Service smoke check failed after sandbox execution.", "evidence": _sandbox_evidence(result)}
    if result.browser_check_attempted and not result.browser_loaded:
        return {"status": "Fail", "reason": "Browser smoke check failed after service startup.", "evidence": _sandbox_evidence(result)}

    failure, warnings = _measured_policy_findings(state, result)
    state.metrics["policy_warnings"] = warnings
    for warning in warnings:
        state.events.append(f"Judge: warning {warning}")
    if failure:
        return {"status": "Fail", "reason": failure, "evidence": _sandbox_evidence(result)}
    return {"status": "Pass", "reason": "Repository passed preflight and sandbox smoke validation.", "evidence": _sandbox_evidence(result)}


def is_real_chaos_observation(result: SandboxResult) -> bool:
    """실제 카오스 실험을 관측한 결과인가.

    `source.real_execution_observed`를 조건에 넣지 않는다. 계약 문서에는 있지만 실제 Litmus
    응답에는 그 필드가 없다(tests/fixtures/chaos_actual). 관측 성공 여부는 observationStatus가
    말해준다.
    """
    return bool(result.chaos_observation) and result.observation_status == "observed"


def chaos_recovered(observation: dict[str, object]) -> bool | None:
    """복구했는지. 모르면 None.

    Litmus 응답에는 `recovered` 불리언이 없고 `recovered_at`만 있다. 키가 없다고 판정 불가로
    보내면 실제 실행이 전부 Error가 된다.
    """
    if "recovered" in observation:
        return bool(observation["recovered"])
    if observation.get("recovered_at"):
        return True
    return None


def chaos_aborted(observation: dict[str, object]) -> bool:
    """중단 조건이 발동했는가. 실측 응답은 {"triggered": false} 객체를 담는다."""
    condition = observation.get("abort_condition")
    if isinstance(condition, dict):
        return bool(condition.get("triggered"))
    return bool(observation.get("aborted"))


def chaos_target_configuration(observation: dict[str, object]) -> dict[str, object]:
    """워크로드 설정. 실측은 target_configuration에 담아 보낸다."""
    config = observation.get("target_configuration")
    return config if isinstance(config, dict) else {}


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
        if chaos_recovered(observation) is False:
            return "chaos_not_recovered: chaos experiment never recovered to a serving state.", warnings

        # 복구 시간은 chaos_observation에도 metrics에도 올 수 있다. 실측은 전자에 담아 보낸다.
        recovery = _as_float(observation.get("recovery_seconds"))
        if recovery is None:
            recovery = _as_float(measured.get("recovery_seconds"))

        bound = _expected_recovery_bound(observation)
        if recovery is not None and bound is not None and recovery > bound:
            return (
                f"chaos_recovery_exceeds_expected_bound: recovery {recovery}s exceeds the bound "
                f"{round(bound, 3)}s implied by the workload configuration.",
                warnings,
            )

        if _as_float(chaos_target_configuration(observation).get("replicas")) == 1:
            # replica가 1개면 다운타임은 문서화된 정상 동작이다. 구성 경고로만 남긴다.
            warnings.append("chaos_single_replica_topology")

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

    # 카오스 실험이 아닌 실측 구간에는 SLO를 그대로 적용한다.
    error_rate = _as_float(measured.get("error_rate"))
    if error_rate is not None and slo.error_rate_max is not None and error_rate > slo.error_rate_max:
        return f"error_rate_slo_violation: error_rate {error_rate} exceeds {slo.error_rate_max}.", warnings

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

    p95 = _as_float(measured.get("p95_latency_ms"))
    if p95 is not None and slo.p95_latency_ms_max is not None and p95 > slo.p95_latency_ms_max:
        return f"latency_slo_violation: p95 {p95}ms exceeds {slo.p95_latency_ms_max}ms.", warnings

    return None, warnings


def _expected_recovery_bound(observation: dict[str, object]) -> float | None:
    """워크로드 설정에서 기대 복구 상한을 계산한다. docs/judge-policy.md 6.5.

    bound = grace + initial_delay + period x success_threshold + startup_allowance
    force 삭제는 grace를 기다리지 않는다. 설정이 없으면 None을 돌려 이 규칙을 건너뛴다.
    근거 없는 상한으로 Fail을 내면 안 된다.
    """
    config = chaos_target_configuration(observation)
    probe = config.get("readiness_probe")
    if not isinstance(probe, dict):
        return None
    period = _as_float(probe.get("period_seconds"))
    success_threshold = _as_float(probe.get("success_threshold"))
    if period is None or success_threshold is None:
        return None

    initial_delay = _as_float(probe.get("initial_delay_seconds")) or 0.0
    grace = 0.0
    if str(observation.get("kill_method", "")).endswith("force"):
        grace = 0.0
    else:
        grace = _as_float(config.get("termination_grace_period_seconds")) or 0.0
    min_ready = _as_float(config.get("min_ready_seconds")) or 0.0
    allowance = get_settings().chaos_recovery_startup_allowance_seconds
    return grace + initial_delay + period * success_threshold + min_ready + allowance


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
            "evidence": _sandbox_evidence(result),
            "recommended_action": "Reduce blocking startup/test work, add timeout-safe startup behavior, and re-run validation from the same commit.",
        }
    if result and result.exit_code not in (0, None):
        return {
            "issue": "Repository command returned a non-zero sandbox exit code.",
            "root_cause": state.judge_report.get("reason", "Sandbox command failed."),
            "evidence": _sandbox_evidence(result),
            "recommended_action": "Fix the failing build/test/run command surfaced in the logs and verify the command exits with exit_code=0.",
        }
    if result and result.service_check_attempted and not _service_smoke_passed(result):
        return {
            "issue": "Service smoke validation failed after the process started.",
            "root_cause": f"HTTP/browser service check failed with http_status={result.http_status} and browser_loaded={result.browser_loaded}.",
            "evidence": _sandbox_evidence(result),
            "recommended_action": "Fix the app health endpoint or start command, then verify the service returns a successful HTTP status and browser probe loads.",
        }
    if result and result.browser_check_attempted and not result.browser_loaded:
        return {
            "issue": "Browser smoke validation failed.",
            "root_cause": "The service did not load successfully in the browser probe.",
            "evidence": _sandbox_evidence(result),
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


def _sandbox_failure_reason(result: SandboxResult) -> str:
    """실패 이유를 한 문장으로. 로그 원문은 evidence 쪽에 따로 들어간다.

    스택을 모를 때 "unknown"을 문장에 넣으면 진단이 모호해지므로 생략한다.
    """
    report = result.sandbox_report or {}
    stack = report.get("detected_stack")
    suffix = f" in the {stack} build" if stack and stack != "unknown" else ""
    failed_step = report.get("failed_step")
    if failed_step and failed_step != "none":
        return f"Sandbox step '{failed_step}' failed with exit_code={result.exit_code}{suffix}."
    return f"Sandbox command exited with exit_code={result.exit_code}{suffix}."


def _sandbox_evidence(result: SandboxResult) -> list[str]:
    """로그 전문 대신 구조화된 사실 + 짧은 발췌만 남긴다.

    각 항목은 evidence 패킷의 evidence_refs와 같은 형식·같은 절단 길이를 쓴다.
    그래야 Critic이 인용한 근거가 패킷 안에서 그대로 확인된다.
    """
    items = [
        f"exit_code={result.exit_code}",
        f"timed_out={result.timed_out}",
        f"duration_ms={result.duration_ms}",
        f"http_status={result.http_status}",
        f"browser_loaded={result.browser_loaded}",
    ]
    if result.sandbox_summary:
        items.append(result.sandbox_summary)
    if result.stdout.strip():
        items.append(truncate_log(result.stdout.strip(), 400))
    if result.stderr.strip():
        items.append(truncate_log(result.stderr.strip(), 400))
    return items


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
