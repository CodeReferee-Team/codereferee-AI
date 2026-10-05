import json
from typing import Any

from pydantic import ValidationError

from app.agents.evidence import (
    build_evidence_packet,
    build_refiner_evidence,
    classify_failure_category,
    render_evidence_packet,
    truncate_log,
)
from app.agents.patching import apply_edits, build_diff, inspect_diff, inspect_rewrite
from app.agents.llm import llm, parse_json_strict
from app.agents.prompts import CRITIC_PROMPT, JUDGE_PROMPT, PLANNER_PROMPT, REFINER_PROMPT
from app.agents.schemas import CriticReport, JudgeReport, PlannerReport, RefinerReport, StrictAgentReport, validate_report
from app.config import get_settings
from app.models import SLO, DEFAULT_SLO, AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.sandbox.docker_runner import (
    NO_MANIFEST_EXIT_CODE,
    NOTHING_TO_VERIFY_EXIT_CODE,
    PATCH_MISSING_EXIT_CODE,
    UNSUPPORTED_STACK_EXIT_CODE,
)
from app.config import get_settings


# 카오스 구간 p95가 baseline의 몇 배를 넘으면 경고할지. 출처 있는 값이 아니라 우리 관례다
# (docs/judge-policy.md 6.3). Sandbox가 p99를 보내기 시작하면 임계값을 다시 정한다.
# 우리가 정한 값이다. 출처가 없다. Sandbox가 p95만 보내서 임시로 쓰는 배수이고,
# SRE Book은 p95 단일 임계값이 아니라 p50과 p99를 함께 보라고 권고한다.
# docs/judge-policy.md 9.2·9.3. p50·p99가 들어오면 이 값을 다시 정한다.
LATENCY_DEGRADATION_FACTOR = 10


def planner_node(state: AgentState) -> AgentState:
    state.events.append("Planner: repository validation plan prepared")
    packet = build_evidence_packet(state)
    fallback = _fallback_plan(state)
    if llm.enabled and get_settings().planner_uses_llm:
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
    if llm.enabled and get_settings().judge_uses_llm:
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
    packet = build_refiner_evidence(state)
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

    _diff_from_edits(state)
    if llm.enabled:
        _retry_rejected_edits(state, packet)
    _record_patch_inspection(state)
    return state


# 재요청은 한 번만 한다. 같은 실수를 반복하는 모델에 호출을 계속 쓸 이유가 없다.
RETRYABLE_EDIT_REJECTIONS = ("edit_anchor_not_found", "edit_anchor_ambiguous", "edit_path_unknown")


def _retry_rejected_edits(state: AgentState, packet: dict[str, object]) -> None:
    """편집이 거부되면 그 이유를 들고 한 번만 다시 묻는다.

    거부 이유는 우리가 파일과 대조해 만든 결정적 신호다(앵커가 없다/여럿이다/모르는 파일이다).
    모델에게 무엇이 어긋났는지 알려주면 고칠 수 있다. 스키마 수리와 같은 구조다.
    """
    check = state.metrics.get("patch_check") or {}
    if check.get("reason_code") != "edits_not_applicable":
        return
    reason = str(check.get("reason") or "")
    if not any(code in reason for code in RETRYABLE_EDIT_REJECTIONS):
        return

    state.events.append(f"Refiner: retrying edits after {reason}")
    retry = _invoke_validated_report(
        role="Refiner",
        schema=RefinerReport,
        system_prompt=REFINER_PROMPT,
        user_prompt=(
            "Your previous edits were thrown away: {reason}\n"
            "edit_anchor_not_found means the lines in find do not exist in the file. "
            "edit_anchor_ambiguous means they appear more than once, so add an adjacent line. "
            "edit_path_unknown means that path is not in evidence.source_files.\n"
            "Copy find character for character from evidence.source_files below. Do not use log text.\n"
            "Previous find values: {anchors}\n"
            "Repository: {repository_url}\nEvidence packet:\n{evidence}"
        ),
        values={
            "reason": reason,
            "anchors": json.dumps(check.get("attempted_anchors") or [], ensure_ascii=False)[:600],
            "repository_url": state.repository_url,
            "evidence": render_evidence_packet(packet),
        },
        fallback=state.refiner_report or _fallback_refiner(state),
        events=state.events,
    )
    if not retry.get("edits"):
        return
    # 재시도 결과로 갈아끼운 뒤 같은 검증을 다시 지난다.
    state.refiner_report = retry
    state.metrics.pop("patch_check", None)
    _diff_from_edits(state)
    outcome = (state.metrics.get("patch_check") or {}).get("reason_code")
    state.events.append(
        "Refiner: retry produced an applicable edit" if not outcome else f"Refiner: retry still rejected ({outcome})"
    )


def _diff_from_edits(state: AgentState) -> None:
    """모델이 준 편집 목록을 적용해 diff를 만든다.

    치환은 우리가 하므로 모델이 파일의 다른 부분을 건드릴 수 없다. 전문을 받던 방식에서는
    모델이 뒤를 잘라먹어 멀쩡한 코드가 지워졌다(docs/evaluation-design.md 14.7).
    """
    edits = state.refiner_report.pop("edits", None)
    if not isinstance(edits, list) or not edits:
        return
    outcome = apply_edits(state.source_files, [dict(edit) for edit in edits])
    if outcome.rejected:
        state.events.append(f"Refiner: edits rejected ({', '.join(outcome.rejected)})")
    if not outcome.patched:
        state.metrics["patch_check"] = {
            "accepted": False,
            "reason_code": "edits_not_applicable",
            "reason": ", ".join(outcome.rejected) or "no edit changed a file",
            # 모델이 무엇을 앵커로 썼는지 남긴다. 프롬프트를 고칠 근거가 된다.
            "attempted_anchors": [str(edit.get("find")) [:200] for edit in edits if isinstance(edit, dict)],
        }
        return
    diff = build_diff(state.source_files, outcome.patched)
    state.refiner_report["patched_paths"] = sorted(outcome.patched)
    if diff:
        rewrite = inspect_rewrite(diff, state.source_files)
        if not rewrite.accepted:
            # 모델이 파일 뒤를 잘라먹은 경우다. 적용되더라도 멀쩡한 코드를 지운다.
            state.events.append(f"Refiner: patch rejected as a rewrite: {rewrite.reason}")
            state.metrics["patch_check"] = {
                "accepted": False,
                "reason_code": rewrite.reason_code,
                "reason": rewrite.reason,
                "touched_paths": rewrite.touched_paths,
            }
            state.refiner_report["patch_diff"] = None
            return
    state.refiner_report["patch_diff"] = diff or None


def _record_patch_inspection(state: AgentState) -> None:
    """생성된 패치를 내용 기준으로 먼저 거른다. 적용 검사(git apply)는 워크플로가 레포를 받은 뒤 한다."""
    diff = state.refiner_report.get("patch_diff")
    if not diff:
        # 앞 단계가 구체적인 이유를 남겼으면 덮지 않는다. patch_absent로 덮으면
        # 편집이 왜 거부됐는지(앵커 불일치, 재작성 등)가 사라진다.
        existing = state.metrics.get("patch_check") or {}
        if not existing.get("reason_code"):
            state.metrics["patch_check"] = {"accepted": False, "reason_code": "patch_absent"}
        return
    verdict = inspect_diff(str(diff))
    state.metrics["patch_check"] = {
        "accepted": verdict.accepted,
        "reason_code": verdict.reason_code,
        "reason": verdict.reason,
        "touched_paths": verdict.touched_paths,
    }
    if not verdict.accepted:
        state.events.append(f"Refiner: patch rejected before sandbox: {verdict.reason_code}")
        state.refiner_report["patch_diff"] = None


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
    try:
        raw = llm.invoke_text(system_prompt, user_prompt, values)
    except Exception as exc:
        # 전송 실패(타임아웃, 연결 거부, 5xx). 판정은 규칙이 이미 냈고 여기서 만드는 것은
        # 그 위에 얹는 서술과 수정안이다. 모델에 닿지 못했다고 작업 전체를 버리면
        # 검증 결과가 LLM 가용성에 묶인다.
        events.append(f"{role}: LLM unreachable, deterministic fallback selected: {_event_error(exc)}")
        return validate_report(schema, fallback)
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
        return {"status": "Fail", "reason_category": "timeout", "reason": "Sandbox execution timed out.", "evidence": _sandbox_evidence(result)}
    if result.exit_code != 0:
        return {"status": "Fail", "reason_category": _nonzero_exit_category(result), "reason": _sandbox_failure_reason(result), "evidence": _sandbox_evidence(result)}
    if result.service_check_attempted and not _service_smoke_passed(result):
        return {"status": "Fail", "reason_category": "service_smoke_failed", "reason": "Service smoke check failed after sandbox execution.", "evidence": _sandbox_evidence(result)}
    if result.browser_check_attempted and not result.browser_loaded:
        return {"status": "Fail", "reason_category": "browser_smoke_failed", "reason": "Browser smoke check failed after service startup.", "evidence": _sandbox_evidence(result)}

    failure, warnings = _measured_policy_findings(state, result)
    state.metrics["policy_warnings"] = warnings
    for warning in warnings:
        state.events.append(f"Judge: warning {warning}")
    if failure:
        # 규칙이 만든 문장은 "카테고리: 설명" 형태다. 앞부분을 그대로 카테고리로 쓴다.
        category, _, detail = failure.partition(": ")
        return {"status": "Fail", "reason_category": category, "reason": detail or failure, "evidence": _sandbox_evidence(result)}
    passed_category = "chaos_recovered_within_budget" if result.chaos_observation else "all_checks_passed"
    return {"status": "Pass", "reason_category": passed_category, "reason": "Repository passed preflight and sandbox smoke validation.", "evidence": _sandbox_evidence(result)}


def _preflight_category(preflight: RepositoryPreflightReport) -> str:
    """clone 실패의 원인을 preflight가 남긴 문장에서 가른다."""
    text = f"{preflight.reason} {' '.join(preflight.evidence)}".lower()
    if "not found" in text or "404" in text:
        return "repository_not_found"
    if "ref" in text or "branch" in text or "commit" in text:
        return "ref_not_found"
    if "private" in text or "auth" in text or "permission" in text:
        return "private_repository_not_supported"
    if "invalid" in text or "not a github" in text or "url" in text:
        return "invalid_repository_input"
    return "repository_not_accessible"


def _no_entrypoint_category(preflight: RepositoryPreflightReport) -> str:
    """실행 경로가 없는 이유를 가른다. 매니페스트가 없는 것과 고를 수 없는 것은 다르다."""
    text = f"{preflight.reason} {' '.join(preflight.evidence)}".lower()
    if "empty" in text:
        return "empty_repository"
    if "monorepo" in text or "multiple" in text or "ambiguous" in text:
        return "ambiguous_monorepo_path"
    if "unsupported" in text or "stack" in text:
        return "unsupported_project_stack"
    return "no_manifest_detected"


# sandbox가 구조화 결과로 알려주는 실패 단계 -> 판정 카테고리.
# 로그 문자열을 뒤지는 것보다 정확하다. 외부 sandbox가 이 리포트를 보내지 않을 때만 키워드로 내려간다.
_FAILED_STEP_CATEGORIES = {
    "prepare": "sandbox_not_executed",
    "clone": "repository_not_accessible",
    "patch": "sandbox_nonzero_exit",
    "detect": "no_manifest_detected",
    "dependencies": "dependency_install_failed",
}
# 스크립트가 약속한 종료 코드. 사유 문장(_EXIT_CODE_REASONS)과 짝을 맞춘다.
# 88(패치 없음)은 우리가 패치를 못 넣은 것이라 레포 탓이 아니고, 재검증 라운드에서만 나오므로
# 여기 넣지 않는다. 그 경로는 patch_check가 기록한다.
_EXIT_CODE_CATEGORIES = {
    NO_MANIFEST_EXIT_CODE: "no_manifest_detected",
    UNSUPPORTED_STACK_EXIT_CODE: "unsupported_project_stack",
    NOTHING_TO_VERIFY_EXIT_CODE: "no_tests_detected",
}
# pip이 실제로 찍는 해결 실패 문구만 본다. "install"은 성공 로그에도 나온다.
_DEPENDENCY_SIGNS = (
    "no matching distribution found",
    "could not find a version that satisfies",
    "resolutionimpossible",
    "npm err!",
    "could not resolve dependencies",
)
_TEST_SIGNS = ("failures ===", "short test summary", "assertionerror", "[test]")
_TEST_TOKENS = ("test", "pytest", "spec")
_FAILURE_TOKENS = ("fail", "error")
# 컴파일 실패 전용 카테고리는 없다. 원인을 단정하지 않고 일반 실패로 남긴다.
_COMPILE_SIGNS = ("error compiling", "syntaxerror", "indentationerror")


def _nonzero_exit_category(result: SandboxResult) -> str:
    """0이 아닌 종료를 판정 카테고리로 옮긴다.

    sandbox가 보낸 구조화 결과(exit code, failed_step)를 먼저 본다. 그것이 없을 때만
    로그 문구로 내려간다. 로그 전체를 substring으로 뒤지면 준비 과정 출력에 걸린다.
    """
    if category := _EXIT_CODE_CATEGORIES.get(result.exit_code):
        return category

    report = result.sandbox_report or {}
    failed_step = str(report.get("failed_step") or "")
    if category := _FAILED_STEP_CATEGORIES.get(failed_step):
        return category
    if failed_step == "smoke":
        return _smoke_category(result)
    return _smoke_category(result)


def _smoke_category(result: SandboxResult) -> str:
    text = f"{result.stderr} {result.stdout}".lower()
    if "docker" in text:
        return "docker_build_failed"
    if any(sign in text for sign in _DEPENDENCY_SIGNS):
        return "dependency_install_failed"
    if any(sign in text for sign in _COMPILE_SIGNS):
        return "sandbox_nonzero_exit"
    if any(sign in text for sign in _TEST_SIGNS):
        return "test_failure"
    if any(t in text for t in _TEST_TOKENS) and any(f in text for f in _FAILURE_TOKENS):
        return "test_failure"
    return "sandbox_nonzero_exit"


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
    if (finding := _unmeasurable_reason(measured, slo)) is not None:
        return finding, warnings

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

    return _resource_reason(measured, slo), warnings


# (사유 코드, 관측 키, SLO 임계 필드, 단위). 상한을 넘으면 Fail이다.
_RESOURCE_CEILINGS = (
    ("cpu_saturation", "cpu_usage_percent", "cpu_usage_percent_max", "%"),
    ("unexpected_restart", "restart_count", "restart_count_max", " restarts"),
    ("database_connection_errors", "db_connection_errors", "db_connection_errors_max", " errors"),
    ("redis_connection_errors", "redis_connection_errors", "redis_connection_errors_max", " errors"),
)
# 이 중 하나라도 측정되면 판정할 근거가 있다고 본다.
_JUDGEABLE_KEYS = ("error_rate", "availability", "p95_latency_ms", "p99_latency_ms", "cpu_usage_percent")


def _unmeasurable_reason(measured: dict[str, object], slo: SLO) -> str | None:
    """판정 근거가 될 지표가 하나도 없으면 통과로 보내지 않는다.

    트래픽이 0건이면 가용성 100%와 오류율 0%는 아무것도 뜻하지 않는다. 키가 아예 없는
    경우(sandbox가 그 지표를 안 보냄)와 키는 있는데 값이 null인 경우(측정에 실패함)를
    구분해서, 뒤쪽만 Fail로 본다.
    """
    requests = _as_float(measured.get("request_count"))
    if requests is not None and slo.request_count_min is not None and requests < slo.request_count_min:
        return (
            f"no_traffic_observed: request_count {requests} is below the minimum "
            f"{slo.request_count_min}, so the other metrics describe nothing."
        )

    present = [key for key in _JUDGEABLE_KEYS if key in measured]
    if present and all(_as_float(measured.get(key)) is None for key in present):
        return (
            "missing_metrics: the sandbox reported the metric keys but no values, "
            "so none of the targets could be checked."
        )
    return None


def _resource_reason(measured: dict[str, object], slo: SLO) -> str | None:
    """자원 지표가 상한을 넘었는지. 운영자가 임계값을 정한 지표만 본다."""
    for category, metric_key, slo_field, unit in _RESOURCE_CEILINGS:
        observed = _as_float(measured.get(metric_key))
        ceiling = getattr(slo, slo_field)
        if observed is not None and ceiling is not None and observed > ceiling:
            return f"{category}: {metric_key} {observed}{unit} exceeds {ceiling}{unit}."

    ratio = _memory_usage_ratio(measured)
    if ratio is not None and slo.memory_usage_ratio_max is not None and ratio > slo.memory_usage_ratio_max:
        return (
            f"memory_pressure: memory usage {round(ratio, 3)} of the limit exceeds "
            f"{slo.memory_usage_ratio_max}."
        )
    return None


def _memory_usage_ratio(measured: dict[str, object]) -> float | None:
    """한도 대비 사용률. sandbox는 사용량과 한도를 MB로 따로 보낸다."""
    if (ratio := _as_float(measured.get("memory_usage_ratio"))) is not None:
        return ratio
    used = _as_float(measured.get("memory_usage_mb"))
    limit = _as_float(measured.get("memory_limit_mb"))
    if used is None or not limit:
        return None
    return used / limit



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


# 스크립트가 약속한 종료 코드의 의미. 숫자만 문장에 넣으면 사용자가 원인을 알 수 없다.
_EXIT_CODE_REASONS = {
    NO_MANIFEST_EXIT_CODE: "No supported project manifest was detected.",
    UNSUPPORTED_STACK_EXIT_CODE: "The repository stack has no runner available in the sandbox.",
    PATCH_MISSING_EXIT_CODE: "The patch file was not present in the sandbox.",
    NOTHING_TO_VERIFY_EXIT_CODE: (
        "The repository has no tests to verify, so its stability could not be checked. "
        "Compiling is not verification."
    ),
}


def _sandbox_failure_reason(result: SandboxResult) -> str:
    """실패 이유를 한 문장으로. 로그 원문은 evidence 쪽에 따로 들어간다.

    스택을 모를 때 "unknown"을 문장에 넣으면 진단이 모호해지므로 생략한다.
    """
    if known := _EXIT_CODE_REASONS.get(result.exit_code):
        return known
    report = result.sandbox_report or {}
    stack = report.get("detected_stack")
    suffix = f" in the {stack} build" if stack and stack != "unknown" else ""
    failed_step = report.get("failed_step")
    # 어느 단계가 실패했는지만 말하면 무엇이 잘못됐는지 알 수 없다. 사용자도, Refiner도 그렇다.
    # 구조화 결과가 준 단계 이름에 실제 실패 문구를 한 줄 붙인다.
    detail = f" {_failure_detail(result)}" if _failure_detail(result) else ""
    if failed_step and failed_step != "none":
        return f"Sandbox step '{failed_step}' failed with exit_code={result.exit_code}{suffix}.{detail}"
    return f"Sandbox command exited with exit_code={result.exit_code}{suffix}.{detail}"


# 판정 이유에 붙일 실패 문구의 길이 상한. 이보다 길면 이유가 로그 덤프가 된다.
MAX_FAILURE_DETAIL_CHARS = 200
# 단계 마커와 진행 표시는 실패 문구가 아니다.
_NOT_A_FAILURE_LINE = ("[CodeReferee]", "detected_stack=", "Cloning into", "stdout:", "stderr:")


def _failure_detail(result: SandboxResult) -> str:
    """실패 문구 한 줄. 도구들은 오류를 마지막에 찍으므로 뒤에서부터 찾는다."""
    for source in (result.stderr, result.stdout):
        for line in reversed(source.splitlines()):
            stripped = line.strip()
            if not stripped or stripped.startswith(_NOT_A_FAILURE_LINE):
                continue
            return stripped[:MAX_FAILURE_DETAIL_CHARS]
    return ""


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
