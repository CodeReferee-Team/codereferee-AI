from __future__ import annotations

import json
from typing import Any

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
        "metrics": summarize_metrics(state.metrics),
        "sre_metrics": state.sre_metrics.model_dump(),
        "judge": dict(state.judge_report),
        "critic": dict(state.critic_feedback),
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
            "observation_status": result.observation_status,
            "infra_error": result.infra_error,
            "run_command": result.run_command,
            "service_check_applicable": getattr(result, "service_check_attempted", False),
            "browser_check_applicable": getattr(result, "browser_check_attempted", False),
            "observation_status": result.observation_status,
            "sandbox_report": dict(result.sandbox_report),
            "sandbox_summary": result.sandbox_summary,
            "log_excerpt": truncate_log(result.log),
        }
    return packet


def build_llm_evidence_packet(state: AgentState) -> dict[str, Any]:
    """Critic/Refiner LLM 전용 축소 패킷.

    전체 패킷(build_evidence_packet)을 주면 7B급 모델이 분석 대신 입력 JSON을 그대로
    되뱉는다(실측). 판정에 실제로 쓰는 몇 필드만 남겨 모델이 '복사'가 아니라 '분석'하게
    한다. 신규 필드를 늘리지 말 것 — 작게 유지하는 게 이 패킷의 존재 이유다.
    """
    judge = dict(state.judge_report or {})
    category = classify_failure_category(state)
    observation = (state.execution_result.chaos_observation if state.execution_result else None) or {}
    config = observation.get("target_configuration") or {}
    packet: dict[str, Any] = {
        "repository_url": state.repository_url,
        "failure_category": category,
        "primary_signal": _primary_signal(state, category),
        "judge": {k: judge.get(k) for k in ("status", "reason_category", "reason")},
        "policy_warnings": list((state.metrics or {}).get("policy_warnings") or []),
    }
    if observation:
        packet["chaos"] = {
            "replicas": config.get("replicas"),
            "recovered": observation.get("recovered"),
            "recovery_seconds": observation.get("recovery_seconds"),
        }
    if state.critic_feedback:
        packet["critic"] = {
            k: state.critic_feedback.get(k) for k in ("root_cause", "recommended_action")
        }
    return packet


def summarize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """프롬프트에 실을 metrics를 줄인다.

    chaos_observation의 kubernetes_events·replacement_logs 등은 수십 KB에 달해, 7B급
    로컬 모델은 이 노이즈에 익사해 판정 신호(replicas·recovery·복구 상한)를 놓친다.
    실측에서 작은 패킷은 단일 replica를 정확히 집는데 큰 패킷은 증상만 복창했다. 그래서
    LLM 패킷에서는 판정에 쓰지 않는 대용량/보조 필드를 뺀다. 원본은 execution_result에
    남아 Backend까지 그대로 전달된다.
    """
    summary = dict(metrics)
    observation = summary.get("chaos_observation")
    if not isinstance(observation, dict):
        return summary

    trimmed = dict(observation)
    events = trimmed.pop("kubernetes_events", None)
    if isinstance(events, list):
        trimmed["kubernetes_events_total"] = len(events)
    for noisy in ("replacement_logs", "fault_parameters", "last_terminations",
                  "target_pod_uid", "replacement_pod_uid", "recovery_measurement"):
        trimmed.pop(noisy, None)
    config = trimmed.get("target_configuration")
    if isinstance(config, dict):
        trimmed["target_configuration"] = {
            key: value for key, value in config.items()
            if key not in ("liveness_probe", "startup_probe")
        }
    summary["chaos_observation"] = trimmed
    return summary


def render_evidence_packet(packet: dict[str, Any]) -> str:
    return json.dumps(packet, ensure_ascii=False, sort_keys=True)


def flatten_evidence_packet(packet: dict[str, Any]) -> str:
    return render_evidence_packet(packet).casefold()


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
    # 카오스 실행은 exit 0로 끝나므로 아래 smoke 규칙이면 "success"가 되어, Judge가 복구
    # 상한 초과로 Fail을 냈는데도 패킷 최상위 신호가 "성공"이라 Critic을 오도한다. Judge가
    # chaos 사유로 떨어뜨렸으면 그 사유를 그대로 failure_category로 쓴다.
    judge = state.judge_report or {}
    reason_category = judge.get("reason_category")
    if judge.get("status") == "Fail" and isinstance(reason_category, str) and reason_category.startswith("chaos_"):
        return reason_category
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
    if category.startswith("chaos_"):
        # 카오스 실패의 결정적 신호는 Judge가 적은 복구/가용성 사유다. exit_code=0이 아니라.
        return str((state.judge_report or {}).get("reason") or category)
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
        if result.stdout.strip():
            refs["log.stdout"] = truncate_log(result.stdout.strip(), 400)
        if result.stderr.strip():
            refs["log.stderr"] = truncate_log(result.stderr.strip(), 400)
        refs["log.combined"] = truncate_log(result.log, 600)
    # 규칙이 계산한 정책 경고(단일 replica 토폴로지, 에러버짓 소진 등)는 구체적 복원력
    # 신호다. evidence_refs에 올려 Critic/Refiner가 원인으로 집게 한다.
    warnings = (state.metrics or {}).get("policy_warnings")
    if isinstance(warnings, list) and warnings:
        refs["policy_warnings"] = " | ".join(str(w) for w in warnings)
    return {key: value for key, value in refs.items() if str(value).strip()}
