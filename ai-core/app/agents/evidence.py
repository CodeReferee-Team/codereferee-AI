from __future__ import annotations

import json
from typing import Any

from app.agents.source_context import drop_progress_lines, failure_region
from app.models import AgentState

MAX_LOG_CHARS = 1200


def build_evidence_packet(state: AgentState) -> dict[str, Any]:
    preflight = state.preflight_report
    result = state.execution_result
    failure_category = classify_failure_category(state)
    evidence_refs = _build_evidence_refs(state, failure_category)
    packet: dict[str, Any] = {
        "schema_version": "agent-evidence.v2",
        "repository_url": state.repository_url,
        "failure_category": failure_category,
        "primary_signal": _primary_signal(state, failure_category),
        "evidence_refs": evidence_refs,
        "secondary_signals": list(evidence_refs.values()),
        "preflight": None,
        "execution": None,
        "metrics": dict(state.metrics),
        "sre_metrics": state.sre_metrics.model_dump(),
        "judge": dict(state.judge_report),
        "critic": dict(state.critic_feedback),
        # 고칠 파일의 현재 내용. 이것 없이는 context가 맞는 diff를 쓸 수 없다.
        "source_files": dict(state.source_files),
    }
    if preflight is not None:
        packet["preflight"] = {
            "cloneable": preflight.cloneable,
            "executable": preflight.executable,
            "detected_stack": preflight.detected_stack,
            "reason": preflight.reason,
            "evidence": list(preflight.evidence),
            "build_command": preflight.build_command,
            "test_command": preflight.test_command,
            "run_command": preflight.run_command,
        }
    if result is not None:
        packet["execution"] = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "server_started": result.server_started,
            "server_url": result.server_url,
            "http_status": result.http_status,
            "browser_loaded": result.browser_loaded,
            "page_title": result.page_title,
            "run_command": result.run_command,
            "service_check_applicable": getattr(result, "service_check_attempted", False),
            "browser_check_applicable": getattr(result, "browser_check_attempted", False),
            # 실패 단계 이후만 발췌한다. 준비 과정을 원인으로 읽는 일이 있었다.
            "log_excerpt": truncate_log(_relevant_log(result.log)),
        }
    return packet


def render_evidence_packet(packet: dict[str, Any]) -> str:
    return json.dumps(packet, ensure_ascii=False, sort_keys=True)


def flatten_evidence_packet(packet: dict[str, Any]) -> str:
    return render_evidence_packet(packet).casefold()


def _relevant_log(log: str) -> str:
    """실패 단계 이후에서 진행 표시를 뺀 부분. evidence의 모든 로그 필드가 이것을 쓴다."""
    return drop_progress_lines(failure_region(log))


def truncate_log(log: str, limit: int = MAX_LOG_CHARS) -> str:
    if len(log) <= limit:
        return log
    head = log[: limit // 2]
    tail = log[-limit // 2 :]
    return f"{head}\n...[truncated]...\n{tail}"


def classify_failure_category(state: AgentState) -> str:
    preflight = state.preflight_report
    if preflight is None:
        return "missing_preflight"
    if not preflight.cloneable:
        return "clone_failure"
    if not preflight.executable:
        return "no_entrypoint"
    result = state.execution_result
    if result is None:
        return "missing_execution"
    if result.timed_out:
        return "timeout"
    if result.exit_code not in (0, None):
        return "non_zero_exit"
    if result.service_check_attempted and not _service_smoke_passed(result):
        return "service_failure"
    if result.browser_check_attempted and not result.browser_loaded:
        return "browser_failure"
    return "success"


def _service_smoke_passed(result: Any) -> bool:
    if result.http_status is not None and not (200 <= result.http_status < 400):
        return False
    return bool(result.server_started or result.server_url or result.http_status is not None)


def _primary_signal(state: AgentState, category: str) -> str:
    preflight = state.preflight_report
    result = state.execution_result
    if category in {"clone_failure", "no_entrypoint"} and preflight is not None:
        return preflight.reason or "; ".join(preflight.evidence) or category
    if result is None:
        return category
    if category == "timeout":
        return f"timed_out=True duration_ms={result.duration_ms}"
    if category == "non_zero_exit":
        return f"exit_code={result.exit_code}"
    if category in {"service_failure", "browser_failure"}:
        return f"http_status={result.http_status} browser_loaded={result.browser_loaded}"
    if category == "success":
        return "exit_code=0 timed_out=False"
    return category


def _build_evidence_refs(state: AgentState, category: str) -> dict[str, str]:
    refs: dict[str, str] = {"category": f"failure_category={category}"}
    preflight = state.preflight_report
    result = state.execution_result
    if preflight is not None:
        refs["preflight.reason"] = preflight.reason
        if preflight.evidence:
            refs["preflight.evidence"] = " | ".join(preflight.evidence)
        refs["preflight.cloneable"] = f"cloneable={preflight.cloneable}"
        refs["preflight.executable"] = f"executable={preflight.executable}"
    if result is not None:
        refs["exec.exit_code"] = f"exit_code={result.exit_code}"
        refs["exec.timed_out"] = f"timed_out={result.timed_out}"
        refs["exec.duration_ms"] = f"duration_ms={result.duration_ms}"
        refs["exec.http_status"] = f"http_status={result.http_status}"
        refs["exec.browser_loaded"] = f"browser_loaded={result.browser_loaded}"
        refs["exec.service_check_applicable"] = f"service_check_applicable={result.service_check_attempted}"
        refs["exec.browser_check_applicable"] = f"browser_check_applicable={result.browser_check_attempted}"
        # 로그는 전부 실패 단계 이후만 쓴다. 앞에서 자르면 준비 과정(apt-get)이 채우고,
        # 이 값들은 secondary_signals로도 들어가 서두가 packet에 여러 번 반복된다.
        if _relevant_log(result.stdout).strip():
            refs["log.stdout"] = truncate_log(_relevant_log(result.stdout).strip(), 400)
        if _relevant_log(result.stderr).strip():
            refs["log.stderr"] = truncate_log(_relevant_log(result.stderr).strip(), 400)
        refs["log.combined"] = truncate_log(_relevant_log(result.log), 600)
    return {key: value for key, value in refs.items() if str(value).strip()}


def build_refiner_evidence(state: AgentState) -> dict[str, Any]:
    """Refiner가 편집을 쓰는 데 필요한 것만 담는다.

    전체 packet에서는 evidence_refs, sre_metrics, secondary_signals가 59%를 차지하는데 편집과
    아무 상관이 없다. 8B가 그 조각을 리포트로 되받아 적는 것을 관측했다(sre_metrics만 담긴 응답).
    판정은 규칙이 내므로 judge가 권위 있는 서술이다.
    """
    result = state.execution_result
    critic = state.critic_feedback
    return {
        "schema_version": "agent-evidence.refiner.v1",
        "repository_url": state.repository_url,
        "failure_category": classify_failure_category(state),
        "judge": {
            "status": state.judge_report.get("status"),
            "reason_category": state.judge_report.get("reason_category"),
            "reason": state.judge_report.get("reason"),
        },
        "critic": {
            "root_cause": critic.get("root_cause"),
            "recommended_action": critic.get("recommended_action"),
        },
        "execution": {
            "exit_code": result.exit_code if result else None,
            "log_excerpt": truncate_log(_relevant_log(result.log)) if result else "",
        },
        "source_files": dict(state.source_files),
    }
