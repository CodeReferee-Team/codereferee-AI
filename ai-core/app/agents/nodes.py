from typing import Any

from pydantic import ValidationError

from app.agents.evidence import build_evidence_packet, classify_failure_category, render_evidence_packet
from app.agents.llm import llm, parse_json_strict
from app.agents.prompts import CRITIC_PROMPT, JUDGE_PROMPT, PLANNER_PROMPT, REFINER_PROMPT
from app.agents.schemas import CriticReport, JudgeReport, PlannerReport, RefinerReport, StrictAgentReport, validate_report
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


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
    if result.timed_out:
        return {"status": "Fail", "reason": "Sandbox execution timed out.", "evidence": [result.log]}
    if result.exit_code != 0:
        return {"status": "Fail", "reason": result.stderr.strip() or "Sandbox returned non-zero exit code.", "evidence": [result.log]}
    if result.service_check_attempted and not _service_smoke_passed(result):
        return {"status": "Fail", "reason": "Service smoke check failed after sandbox execution.", "evidence": [result.log]}
    if result.browser_check_attempted and not result.browser_loaded:
        return {"status": "Fail", "reason": "Browser smoke check failed after service startup.", "evidence": [result.log]}
    return {"status": "Pass", "reason": "Repository passed preflight and sandbox smoke validation.", "evidence": [result.log]}


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
