import re

from app.agents.llm import llm, parse_json
from app.agents.prompts import (
    CRITIC_PROMPT,
    DRAFT_PROMPT,
    JUDGE_PROMPT,
    PLANNER_PROMPT,
    REFINER_PROMPT,
)
from app.models import AgentState, JobStatus, SandboxResult


def planner_node(state: AgentState) -> AgentState:
    state.events.append("Planner: requirement analyzed")
    if llm.enabled:
        raw = llm.invoke_text(
            PLANNER_PROMPT,
            "User requirement: {requirement}",
            {"requirement": state.requirement},
        )
        state.plan = parse_json(raw, _fallback_plan(state.requirement))
    else:
        state.plan = _fallback_plan(state.requirement)
    return state


def draft_node(state: AgentState) -> AgentState:
    state.events.append("Draft: initial code generated")
    if llm.enabled:
        state.current_code = llm.invoke_text(
            DRAFT_PROMPT,
            "Requirement: {requirement}\nPlan: {plan}",
            {"requirement": state.requirement, "plan": state.plan},
        )
    else:
        state.current_code = _fallback_code(state.requirement)
    return state


def critic_node(state: AgentState) -> AgentState:
    state.events.append("Critic: reliability review completed")
    log = state.execution_result.log if state.execution_result else "No execution yet."
    if llm.enabled:
        raw = llm.invoke_text(
            CRITIC_PROMPT,
            "Current code:\n{code}\n\nSandbox logs:\n{log}",
            {"code": state.current_code, "log": log},
        )
        state.critic_feedback = parse_json(raw, _fallback_feedback(log, state.current_code))
    else:
        state.critic_feedback = _fallback_feedback(log, state.current_code)
    return state


def refiner_node(state: AgentState) -> AgentState:
    state.events.append("Refiner: reliability patch applied")
    if llm.enabled:
        state.current_code = llm.invoke_text(
            REFINER_PROMPT,
            "Existing code:\n{code}\n\nCritic feedback:\n{feedback}",
            {"code": state.current_code, "feedback": state.critic_feedback},
        )
    elif state.execution_result and state.execution_result.timed_out:
        state.current_code = _fallback_code(state.requirement, timeout_seconds=1)
    elif "reliability guardrails" in str(state.critic_feedback.get("issue", "")):
        state.current_code = _fallback_code(
            state.requirement,
            timeout_seconds=2,
            url=_extract_first_url(state.current_code),
        )
    return state


def judge_node(state: AgentState) -> AgentState:
    state.events.append("Judge: sandbox result evaluated")
    result = state.execution_result or SandboxResult(exit_code=None, stderr="No sandbox result")
    if llm.enabled:
        raw = llm.invoke_text(JUDGE_PROMPT, "Sandbox logs:\n{log}", {"log": result.log})
        report = parse_json(raw, _fallback_judge(result))
    else:
        report = _fallback_judge(result)

    state.judge_report = report
    if str(report.get("status", "")).lower() == "pass":
        state.status = JobStatus.success
    else:
        state.status = JobStatus.failed
        state.error_count += 1
    return state


def _fallback_plan(requirement: str) -> dict[str, object]:
    return {
        "objective": requirement,
        "technical_requirements": ["Python 3.12", "requests when HTTP is required"],
        "sre_considerations": ["timeout", "retry", "bounded execution time", "clear errors"],
        "implementation_steps": [
            "Parse the requirement",
            "Implement defensive Python code",
            "Run inside Docker sandbox",
            "Judge result from logs",
        ],
    }


def _fallback_code(requirement: str, timeout_seconds: int = 2, url: str | None = None) -> str:
    if _looks_like_http_requirement(requirement) or url:
        target_url = url or "https://httpstat.us/200?sleep=3000"
        return f'''import time
import urllib.request


def fetch_with_retry(url: str, retries: int = 3, timeout: int = {timeout_seconds}) -> str:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read(4096).decode("utf-8", errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"request failed after {{retries}} attempts: {{last_error}}")


if __name__ == "__main__":
    print(fetch_with_retry("{target_url}"))
'''

    escaped_requirement = requirement.replace("\\", "\\\\").replace('"', '\\"')
    return f'''def main() -> None:
    requirement = "{escaped_requirement}"
    print("CodeReferee fallback execution")
    print(requirement)


if __name__ == "__main__":
    main()
'''


def _looks_like_http_requirement(requirement: str) -> bool:
    lowered = requirement.lower()
    return any(keyword in lowered for keyword in ["http", "api", "url", "request", "network", "외부", "호출"])


def _extract_first_url(code: str) -> str | None:
    match = re.search(r"https?://[^'\"\\s)]+", code)
    return match.group(0) if match else None


def _fallback_feedback(log: str, code: str = "") -> dict[str, str]:
    if "timed_out=True" in log:
        return {"issue": "Execution timed out.", "solution": "Reduce blocking timeouts and retries."}
    lowered = code.lower()
    if any(token in lowered for token in ["urlopen", "requests.", "httpx."]):
        missing = []
        if "timeout" not in lowered:
            missing.append("timeout")
        if "retry" not in lowered and "attempt" not in lowered:
            missing.append("retry")
        if missing:
            return {
                "issue": f"HTTP call is missing reliability guardrails: {', '.join(missing)}.",
                "solution": "Add explicit request timeouts, bounded retries, and clear failure handling.",
            }
    return {"issue": "No critical issue found before execution.", "solution": "Run sandbox validation."}


def _fallback_judge(result: SandboxResult) -> dict[str, str]:
    if result.timed_out:
        return {"status": "Fail", "reason": "Sandbox execution timed out."}
    if result.exit_code != 0:
        return {"status": "Fail", "reason": result.stderr.strip() or "Non-zero exit code."}
    return {"status": "Pass", "reason": "Sandbox finished successfully with exit code 0."}
