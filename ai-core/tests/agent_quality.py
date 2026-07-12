from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import nodes
from app.agents.evidence import build_evidence_packet, flatten_evidence_packet
from app.agents.schemas import CriticReport, JudgeReport, PlannerReport, RefinerReport, validate_report
from app.models import AgentState, RepositoryPreflightReport, SandboxResult

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_golden_cases.json"


def load_cases(path: Path = FIXTURE_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def state_from_case(case: dict[str, Any]) -> AgentState:
    raw = case["state"]
    state = AgentState(job_id=case["id"], repository_url=raw["repository_url"])
    if preflight := raw.get("preflight_report"):
        state.preflight_report = RepositoryPreflightReport(**preflight)
    if execution := raw.get("execution_result"):
        state.execution_result = SandboxResult(**execution)
        state.metrics = {
            "exit_code": state.execution_result.exit_code,
            "timed_out": state.execution_result.timed_out,
            "duration_ms": state.execution_result.duration_ms,
            "http_status": state.execution_result.http_status,
            "browser_loaded": state.execution_result.browser_loaded,
            "service_check_attempted": state.execution_result.service_check_attempted,
            "browser_check_attempted": state.execution_result.browser_check_attempted,
        }
    else:
        state.metrics = {"sandbox_executed": False}
    return state


def run_case(case: dict[str, Any]) -> AgentState:
    previous_enabled = nodes.llm.enabled
    nodes.llm.enabled = False
    try:
        state = state_from_case(case)
        state = nodes.planner_node(state)
        state = nodes.judge_node(state)
        state = nodes.critic_node(state)
        state = nodes.refiner_node(state)
        return state
    finally:
        nodes.llm.enabled = previous_enabled


def evaluate_cases(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cases = cases or load_cases()
    results: list[dict[str, Any]] = []
    totals = {"planner": 0, "judge": 0, "critic": 0, "refiner": 0}
    passed = {"planner": 0, "judge": 0, "critic": 0, "refiner": 0}
    for case in cases:
        state = run_case(case)
        packet_text = flatten_evidence_packet(build_evidence_packet(state))
        case_result = {
            "id": case["id"],
            "planner": _score_planner(case, state, packet_text),
            "judge": _score_judge(case, state),
            "critic": _score_critic(case, state, packet_text),
            "refiner": _score_refiner(case, state),
        }
        for agent in totals:
            totals[agent] += 1
            passed[agent] += int(case_result[agent]["passed"])
        results.append(case_result)
    scores = {agent: passed[agent] / totals[agent] for agent in totals}
    return {"scores": scores, "passed": passed, "totals": totals, "cases": results}


def write_evaluation(path: Path) -> dict[str, Any]:
    result = evaluate_cases()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _score_planner(case: dict[str, Any], state: AgentState, packet_text: str) -> dict[str, Any]:
    validate_report(PlannerReport, state.validation_plan)
    failures: list[str] = []
    for grounding in case.get("planner_grounding", []):
        field = grounding["output_field"]
        output_text = _normalize(" ".join(state.validation_plan.get(field, [])))
        if not any(_normalize(alias) in output_text for alias in grounding["output_aliases"]):
            failures.append(f"planner missing alias in {field}: {grounding['output_aliases']}")
        if _normalize(grounding["input_token"]) not in packet_text:
            failures.append(f"planner missing input provenance token: {grounding['input_token']}")
    return {"passed": not failures, "failures": failures}


def _score_judge(case: dict[str, Any], state: AgentState) -> dict[str, Any]:
    validate_report(JudgeReport, state.judge_report)
    failures: list[str] = []
    if state.judge_report.get("status") != case["judge_status"]:
        failures.append(f"judge status {state.judge_report.get('status')} != {case['judge_status']}")
    return {"passed": not failures, "failures": failures}


def _score_critic(case: dict[str, Any], state: AgentState, packet_text: str) -> dict[str, Any]:
    validate_report(CriticReport, state.critic_feedback)
    text = _normalize(_report_text(state.critic_feedback))
    evidence_text = _normalize(" ".join(state.critic_feedback.get("evidence", [])))
    failures = _missing_concept_failures("critic", case.get("critic_concepts", []), text)
    for token in case.get("evidence_tokens", []):
        normalized = _normalize(token)
        if normalized not in packet_text:
            failures.append(f"critic input token missing from packet: {token}")
        if normalized not in evidence_text:
            failures.append(f"critic evidence missing token: {token}")
    failures.extend(_forbidden_claim_failures("critic", case, text))
    failures.extend(_generic_diagnosis_failures(case, state))
    failures.extend(_hallucinated_evidence_failures(state.critic_feedback.get("evidence", []), packet_text))
    return {"passed": not failures, "failures": failures}


def _score_refiner(case: dict[str, Any], state: AgentState) -> dict[str, Any]:
    validate_report(RefinerReport, state.refiner_report)
    text = _normalize(_report_text(state.refiner_report))
    failures = _missing_concept_failures("refiner", case.get("refiner_concepts", []), text)
    if "replacement project" in text or "rewrite the repository" in text:
        failures.append("refiner suggested replacement project or repository rewrite")
    failures.extend(_forbidden_claim_failures("refiner", case, text))
    return {"passed": not failures, "failures": failures}


def _missing_concept_failures(agent: str, concept_groups: list[list[str]], text: str) -> list[str]:
    failures: list[str] = []
    for group in concept_groups:
        if not any(_normalize(alias) in text for alias in group):
            failures.append(f"{agent} missing concept group: {group}")
    return failures


def _forbidden_claim_failures(agent: str, case: dict[str, Any], text: str) -> list[str]:
    failures: list[str] = []
    for claim in case.get("forbidden_claims", []):
        if _normalize(claim) in text:
            failures.append(f"{agent} made forbidden unsupported claim: {claim}")
    return failures


def _generic_diagnosis_failures(case: dict[str, Any], state: AgentState) -> list[str]:
    if case.get("judge_status") == "Pass":
        return []
    root_cause = _normalize(str(state.critic_feedback.get("root_cause", "")))
    action = _normalize(str(state.critic_feedback.get("recommended_action", "")))
    generic_markers = [
        "unknown",
        "use the sandbox logs and metrics",
        "generic",
        "some issue",
        "something failed",
    ]
    return [
        f"critic used generic diagnosis marker: {marker}"
        for marker in generic_markers
        if marker in root_cause or marker in action
    ]


def _hallucinated_evidence_failures(evidence: list[str], packet_text: str) -> list[str]:
    failures: list[str] = []
    for item in evidence:
        normalized = _normalize(str(item))
        segments = [_normalize(part) for part in re.split(r"[\n|]+", str(item)) if len(_normalize(part)) >= 10]
        grounded = normalized in packet_text or any(segment in packet_text for segment in segments)
        if normalized and not grounded:
            failures.append(f"critic evidence not present in packet: {item[:80]}")
    return failures


def _report_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_report_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_report_text(v) for v in value)
    return str(value)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,;:|[]{}()\"'")


if __name__ == "__main__":
    output = write_evaluation(Path("../.omx/evidence/agent-quality-final.json"))
    print(json.dumps(output["scores"], indent=2))
