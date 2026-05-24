from app.agents.llm import llm, parse_json
from app.agents.prompts import CRITIC_PROMPT, JUDGE_PROMPT, PLANNER_PROMPT, REFINER_PROMPT
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult


def planner_node(state: AgentState) -> AgentState:
    state.events.append("Planner: repository validation plan prepared")
    fallback = _fallback_plan(state)
    if llm.enabled:
        raw = llm.invoke_text(
            PLANNER_PROMPT,
            "Repository: {repository_url}\nPreflight: {preflight}\nMetrics: {metrics}",
            {
                "repository_url": state.repository_url,
                "preflight": state.preflight_report.model_dump() if state.preflight_report else {},
                "metrics": state.metrics,
            },
        )
        state.validation_plan = parse_json(raw, fallback)
    else:
        state.validation_plan = fallback
    return state


def judge_node(state: AgentState) -> AgentState:
    state.events.append("Judge: repository validation result evaluated")
    fallback = _fallback_judge(state)
    if llm.enabled:
        raw = llm.invoke_text(
            JUDGE_PROMPT,
            "Preflight:\n{preflight}\n\nSandbox logs:\n{log}\n\nMetrics:\n{metrics}",
            {
                "preflight": state.preflight_report.model_dump() if state.preflight_report else {},
                "log": state.execution_result.log if state.execution_result else "No sandbox execution.",
                "metrics": state.metrics,
            },
        )
        report = parse_json(raw, fallback)
    else:
        report = fallback

    state.judge_report = report
    if str(report.get("status", "")).lower() == "pass":
        state.status = JobStatus.success
    else:
        state.status = JobStatus.failed
        state.error_count += 1
    return state


def critic_node(state: AgentState) -> AgentState:
    state.events.append("Critic: repository reliability gap analyzed")
    fallback = _fallback_critic(state)
    if llm.enabled:
        raw = llm.invoke_text(
            CRITIC_PROMPT,
            "Preflight:\n{preflight}\n\nSandbox logs:\n{log}\n\nMetrics:\n{metrics}\n\nJudge:\n{judge}",
            {
                "preflight": state.preflight_report.model_dump() if state.preflight_report else {},
                "log": state.execution_result.log if state.execution_result else "No sandbox execution.",
                "metrics": state.metrics,
                "judge": state.judge_report,
            },
        )
        state.critic_feedback = parse_json(raw, fallback)
    else:
        state.critic_feedback = fallback
    return state


def refiner_node(state: AgentState) -> AgentState:
    state.events.append("Refiner: remediation guidance prepared")
    fallback = _fallback_refiner(state)
    if llm.enabled:
        raw = llm.invoke_text(
            REFINER_PROMPT,
            "Repository: {repository_url}\nCritic feedback:\n{critic}\nJudge:\n{judge}",
            {
                "repository_url": state.repository_url,
                "critic": state.critic_feedback,
                "judge": state.judge_report,
            },
        )
        state.refiner_report = parse_json(raw, fallback)
    else:
        state.refiner_report = fallback
    return state


def _fallback_plan(state: AgentState) -> dict[str, object]:
    return {
        "objective": "Validate an existing GitHub repository instead of generating new code.",
        "validation_scope": ["cloneability", "project type detection", "build/test/run smoke", "chaos-readiness signals"],
        "chaos_scenarios": ["bounded execution timeout", "resource limits", "log/error inspection"],
        "metrics_required": ["exit_code", "duration_ms", "timed_out", "stdout/stderr evidence"],
        "stop_conditions": ["uncloneable repository", "no executable entrypoint", "sandbox timeout", "non-zero execution"],
    }


def _fallback_judge(state: AgentState) -> dict[str, object]:
    preflight = state.preflight_report
    if preflight is None:
        return {"status": "Fail", "reason": "No preflight report was produced.", "evidence": []}
    if not preflight.cloneable:
        return {"status": "Fail", "reason": preflight.reason or "Repository cannot be cloned.", "evidence": preflight.evidence}
    if not preflight.executable:
        return {"status": "Fail", "reason": preflight.reason or "Repository has no detected executable path.", "evidence": preflight.evidence}

    result = state.execution_result or SandboxResult(exit_code=None, stderr="No sandbox execution")
    if result.timed_out:
        return {"status": "Fail", "reason": "Sandbox execution timed out.", "evidence": [result.log]}
    if result.exit_code != 0:
        return {"status": "Fail", "reason": result.stderr.strip() or "Sandbox returned non-zero exit code.", "evidence": [result.log]}
    return {"status": "Pass", "reason": "Repository passed preflight and sandbox smoke validation.", "evidence": [result.log]}


def _fallback_critic(state: AgentState) -> dict[str, object]:
    preflight = state.preflight_report
    if preflight and not preflight.cloneable:
        return {
            "issue": "Repository intake failed before sandbox execution.",
            "root_cause": preflight.reason,
            "evidence": preflight.evidence,
            "recommended_action": "Verify the GitHub URL, branch, repository visibility, and network access.",
        }
    if preflight and not preflight.executable:
        return {
            "issue": "Repository has no supported build/test/run entrypoint.",
            "root_cause": preflight.reason,
            "evidence": preflight.evidence,
            "recommended_action": "Add a standard manifest and deterministic validation command, such as pytest, Gradle test, Maven test, or npm test.",
        }
    return {
        "issue": "Repository failed sandbox reliability validation." if state.status == JobStatus.failed else "No critical reliability issue found.",
        "root_cause": state.judge_report.get("reason", "Unknown"),
        "evidence": state.judge_report.get("evidence", []),
        "recommended_action": "Use the sandbox logs and metrics to add timeouts, health checks, resource bounds, or deterministic tests.",
    }


def _fallback_refiner(state: AgentState) -> dict[str, object]:
    return {
        "summary": state.critic_feedback.get("issue", "Repository validation completed."),
        "patch_guidance": [state.critic_feedback.get("recommended_action", "No patch guidance available.")],
        "verification_steps": ["Re-run repository validation from the same commit SHA.", "Confirm sandbox exit_code=0 and timed_out=False."],
        "risk": "medium" if state.status == JobStatus.failed else "low",
    }
