from uuid import uuid4

from prometheus_client import Counter, Histogram

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.agents.nodes import critic_node, judge_node, planner_node, refiner_node
from app.agents import source_context
from app.agents.patching import PatchVerdict, check_applies, inspect_diff
from app.models import (
    DEFAULT_SLO,
    AgentState,
    JobStatus,
    RepositoryValidationRequest,
    RepositoryValidationResponse,
    SandboxResult,
    SREMetrics,
)
from app import events as event_builder
from app.config import get_settings
from app.queue.redis_queue import redis_task_queue
from app.repository.preflight import repository_preflight_runner
from app.sandbox.docker_runner import PATCH_APPLY_EXIT_CODE, sandbox_runner
from app.storage.sqlite_store import job_store, record_validation_artifacts

VALIDATION_COUNTER = Counter("codereferee_repository_validations_total", "Total repository validations", ["status"])
SANDBOX_DURATION = Histogram("codereferee_repository_sandbox_duration_ms", "Repository sandbox duration in ms")
REPOSITORY_VALIDATION_TASK = "repository_validation"


def create_validation_state(request: RepositoryValidationRequest, job_id: str | None = None) -> AgentState:
    return AgentState(
        job_id=job_id or str(uuid4()),
        request_id=request.request_id,
        repository_url=str(request.repository_url),
        branch=request.branch,
        requested_commit_sha=request.commit_sha,
        status=JobStatus.queued,
    )


def enqueue_repository_validation(request: RepositoryValidationRequest, queue=redis_task_queue) -> AgentState:
    """Persist a queued validation job and push its payload to Redis."""
    state = create_validation_state(request)
    state.events.append("Input: GitHub repository URL received")
    state.events.append("Queue: repository validation enqueued")
    job_store.save(state)
    queue.enqueue(_queue_payload(state))
    return state


def process_next_repository_validation(
    queue=redis_task_queue, *, block: bool = False, timeout: int = 0, output_queue=None
) -> AgentState | None:
    """Process one repository-validation task from Redis.

    block=True maps to Redis BLPOP and is intended for worker.py.
    block=False maps to LPOP and is intended for non-blocking HTTP/admin checks.
    """
    payload = queue.dequeue(block=block, timeout=timeout)
    if payload is None:
        return None
    state = _state_from_queue_payload(payload)
    state.events.append("Queue: repository validation dequeued")
    return execute_repository_validation(state, output_queue=output_queue or queue)


def run_repository_validation(request: RepositoryValidationRequest, job_id: str | None = None) -> AgentState:
    """Run validation synchronously, bypassing Redis. Useful for local smoke tests."""
    state = create_validation_state(request, job_id)
    state.events.append("Input: GitHub repository URL received")
    return execute_repository_validation(state)


def execute_repository_validation(state: AgentState, output_queue=redis_task_queue) -> AgentState:
    def emit_progress(step: str, **kwargs) -> None:
        # request_id가 없으면 서버발 요청이 아니다(로컬 동기 호출). 보낼 곳이 없다.
        if state.request_id:
            output_queue.publish(event_builder.progress_event(state, step, **kwargs))

    state.status = JobStatus.running
    state.events.append("Workflow: repository validation started")
    job_store.save(state)

    state.preflight_report = repository_preflight_runner.run(
        state.repository_url,
        branch=state.branch,
        commit_sha=state.requested_commit_sha,
    )
    state.resolved_commit_sha = state.preflight_report.resolved_commit_sha
    state.events.append("Preflight: repository accessibility checked")
    emit_progress(event_builder.PREFLIGHT)
    preflight_passed = _preflight_passed(state.preflight_report)
    state.events.append(f"Preflight: {'passed' if preflight_passed else 'failed'}")
    state = planner_node(state)

    if preflight_passed:
        state.events.append("Sandbox: repository clone and smoke validation started")
        emit_progress(event_builder.BASELINE)
        state.execution_result = sandbox_runner.run_repository(
            state.preflight_report.repository_url,
            branch=state.branch,
            commit_sha=state.requested_commit_sha,
        )
        state.metrics = _metrics_from_execution(state)
        state.sre_metrics = _sre_metrics_from_execution(state)
        SANDBOX_DURATION.observe(state.execution_result.duration_ms)
        state.events.append("Sandbox: repository clone and smoke validation finished")
        if state.execution_result.chaos_observation:
            # 카오스는 sandbox 안에서 일어나므로 실시간 보고가 불가능하다.
            # 실제로 실험이 있었을 때만 사후에 알린다. 없었다면 보내지 않는다.
            emit_progress(event_builder.CHAOS, detail=str(state.execution_result.chaos_observation.get("type")))
    else:
        state.events.append("Sandbox: skipped because preflight failed")
        state.metrics = _metrics_from_execution(state)
        state.sre_metrics = _sre_metrics_from_execution(state)

    infra_error = _infra_error_reason(state)
    if infra_error:
        # 인프라 문제로 판정 자체가 불가능하다. Judge/Critic/Refiner를 건너뛴다.
        # 멀쩡한 사용자 코드를 두고 Critic이 고칠 곳을 찾게 두면 안 되기 때문이다.
        state.status = JobStatus.error
        state.events.append(f"Workflow: infra error, judgement skipped: {infra_error}")
    else:
        emit_progress(event_builder.JUDGING)
        state = judge_node(state)
        attach_source_files(state)
        state = critic_node(state)
        state = refiner_node(state)
        _verify_patch_applies(state)
        if _patch_is_applicable(state):
            _run_patch_rounds(state, emit_progress)
    VALIDATION_COUNTER.labels(status=state.status).inc()
    _record_sqlite_artifacts(state)
    job_store.save(state)
    if state.request_id:
        # 종료 이벤트는 정확히 한 번. 이 뒤로는 어떤 progress도 보내지 않는다.
        output_queue.publish(event_builder.result_event(state))
    return state


def to_response(state: AgentState, request_id: str | None = None) -> RepositoryValidationResponse:
    return RepositoryValidationResponse(
        request_id=request_id or state.request_id,
        job_id=state.job_id,
        status=state.status,
        repository_url=state.repository_url,
        branch=state.branch,
        commit_sha=state.resolved_commit_sha or state.requested_commit_sha,
        validation_plan=state.validation_plan,
        preflight_report=state.preflight_report,
        execution_result=state.execution_result,
        judge_report=state.judge_report,
        critic_feedback=state.critic_feedback,
        refiner_report=state.refiner_report,
        metrics=state.metrics,
        sre_metrics=state.sre_metrics,
        events=state.events,
    )


def _queue_payload(state: AgentState) -> dict[str, str | None]:
    # Match the server Redis schema so POST /jobs and server-originated tasks are interchangeable.
    return {
        "taskId": state.job_id,
        "repositoryUrl": state.repository_url,
        "branch": state.branch,
        "commitSha": state.requested_commit_sha,
        "submittedAt": None,
    }


def _state_from_queue_payload(payload: dict) -> AgentState:
    if "repositoryUrl" in payload or "taskId" in payload:
        job_id = payload.get("taskId")
        repository_url = payload.get("repositoryUrl")
        branch = payload.get("branch")
        commit_sha = payload.get("commitSha")
        request_id = payload.get("taskId")
        source = "server"
    elif payload.get("type") == REPOSITORY_VALIDATION_TASK:
        job_id = payload.get("job_id")
        repository_url = payload.get("repository_url")
        branch = payload.get("branch")
        commit_sha = payload.get("commit_sha")
        request_id = payload.get("request_id") or payload.get("job_id")
        source = "ai"
    else:
        raise ValueError(f"Unsupported queue task schema: {sorted(payload.keys())}")

    if not job_id or not repository_url:
        raise ValueError("Repository validation task requires taskId/job_id and repositoryUrl/repository_url")

    request = RepositoryValidationRequest(
        repository_url=repository_url,
        branch=branch,
        commit_sha=commit_sha,
        request_id=request_id,
    )
    state = create_validation_state(request, job_id=job_id)
    if submitted_at := payload.get("submittedAt"):
        state.events.append(f"Queue: submittedAt={submitted_at}")
    state.events.append(f"Queue: payload schema={source}")
    return state


def _preflight_passed(report) -> bool:
    return bool(report and report.cloneable and report.executable)


def _infra_error_reason(state: AgentState) -> str | None:
    """CodeReferee 쪽 문제로 판정이 불가능한 경우의 사유를 돌려준다.

    구분 기준은 "사용자 레포를 실행해보고 실패했는가"다. 실행 자체를 못 해봤으면 인프라 문제다.
    docs/judge-policy.md 6.2의 규칙 1, 2에 해당한다.
    """
    if state.preflight_report and state.preflight_report.infra_error:
        return state.preflight_report.infra_error

    result = state.execution_result
    if result is None:
        return None
    if result.infra_error:
        return result.infra_error

    observation = result.chaos_observation
    if not observation:
        return None
    if observation.get("aborted") or result.source.get("aborted"):
        return "chaos_experiment_aborted"
    if not result.baseline or "recovered" not in observation:
        # 정상 상태 대비 편차로 판정하는데 baseline이나 복구 관측이 없으면 판정 근거가 없다.
        return "chaos_evidence_missing"
    return None


def _metrics_from_execution(state: AgentState) -> dict[str, object]:
    preflight_passed = _preflight_passed(state.preflight_report)
    result = state.execution_result
    if result is None:
        return {
            "cloneable": bool(state.preflight_report and state.preflight_report.cloneable),
            "preflight_passed": preflight_passed,
            "sandbox_executed": False,
        }
    metrics = {
        "cloneable": bool(state.preflight_report and state.preflight_report.cloneable),
        "preflight_passed": preflight_passed,
        "sandbox_executed": True,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "server_started": result.server_started,
        "server_url": result.server_url,
        "http_status": result.http_status,
        "browser_loaded": result.browser_loaded,
        "page_title": result.page_title,
        "service_check_attempted": result.service_check_attempted,
        "browser_check_attempted": result.browser_check_attempted,
    }
    metrics.update(result.metrics)
    if result.schema_version:
        metrics["schema_version"] = result.schema_version
    if result.probe_transport:
        metrics["probe_transport"] = result.probe_transport
    if result.baseline:
        metrics["baseline"] = result.baseline
    if result.chaos_observation:
        metrics["chaos_observation"] = result.chaos_observation
    if result.source:
        metrics["source"] = result.source
    return metrics

def _sre_metrics_from_execution(state: AgentState) -> SREMetrics:
    result = state.execution_result
    if result is None:
        preflight_failed = bool(state.preflight_report and not _preflight_passed(state.preflight_report))
        return SREMetrics(
            sli={
                "availability_percent": 0.0 if preflight_failed else None,
                "error_rate": 1.0 if preflight_failed else None,
            },
            slo=_default_slo(),
            error_budget={
                "allowed_error_rate": 0.01,
                "observed_error_rate": 1.0 if preflight_failed else None,
                "budget_remaining_percent": 0.0 if preflight_failed else None,
            },
        )

    successful = (
        result.exit_code == 0
        and not result.timed_out
        and (not result.service_check_attempted or 200 <= (result.http_status or 0) < 400)
        and (not result.browser_check_attempted or result.browser_loaded)
    )
    # Sandbox가 실측한 값이 있으면 그것을 쓰고, 없을 때만 exit code 기반 추정값으로 채운다.
    measured = result.metrics
    observed_error_rate = _measured_float(measured, "error_rate")
    if observed_error_rate is None:
        observed_error_rate = 0.0 if successful else 1.0
    budget_remaining = max(0.0, (0.01 - observed_error_rate) / 0.01 * 100)

    measured_availability = _measured_float(measured, "availability")
    availability_percent = (
        measured_availability * 100 if measured_availability is not None else (100.0 if successful else 0.0)
    )
    p95 = _measured_float(measured, "p95_latency_ms")
    p99 = _measured_float(measured, "p99_latency_ms")
    recovery_seconds = _measured_float(measured, "recovery_seconds")
    observation = result.chaos_observation
    recovered = observation.get("recovered") if "recovered" in observation else successful

    return SREMetrics(
        chaos={
            "scenario": observation.get("type") or "smoke_validation",
            "target": result.source.get("fixture") or "repository_sandbox",
            "duration_sec": max(1, round(result.duration_ms / 1000)),
            "recovered": recovered,
            "recovery_time_sec": recovery_seconds
            if recovery_seconds is not None
            else round(result.duration_ms / 1000, 3),
        },
        sli={
            "availability_percent": availability_percent,
            "p95_latency_ms": p95 if p95 is not None else float(result.duration_ms),
            "p99_latency_ms": p99 if p99 is not None else float(result.duration_ms),
            "error_rate": observed_error_rate,
            "throughput_rps": 1.0 if result.duration_ms <= 0 else round(1000 / result.duration_ms, 3),
        },
        slo=_default_slo(),
        error_budget={
            "allowed_error_rate": 0.01,
            "observed_error_rate": observed_error_rate,
            "budget_remaining_percent": budget_remaining,
        },
    )


def _measured_float(metrics: dict[str, object], key: str) -> float | None:
    """Sandbox가 실제로 관측해 보낸 수치만 반환한다. 없거나 숫자가 아니면 None."""
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _default_slo() -> dict[str, float]:
    return DEFAULT_SLO.model_dump()


def _verify_patch_applies(state: AgentState) -> None:
    """Refiner 패치가 실제 레포에 적용되는지 확인한다.

    sandbox에 보내기 전에 여기서 거른다. 적용도 안 될 패치로 sandbox를 돌리면
    수십 초를 버리고, 실패 원인이 패치인지 코드인지도 흐려진다.
    """
    diff = state.refiner_report.get("patch_diff")
    if not diff:
        return
    verdict = _check_applies(state, str(diff))
    if verdict is None:
        return

    check = dict(state.metrics.get("patch_check") or {})
    check.update({"applies": verdict.accepted, "reason_code": verdict.reason_code, "reason": verdict.reason})
    state.metrics["patch_check"] = check
    if verdict.accepted:
        state.events.append("Refiner: patch applies cleanly to the repository")
    else:
        state.events.append(f"Refiner: patch does not apply: {verdict.reason_code}")
        state.refiner_report["patch_diff"] = None


def attach_source_files(state: AgentState, applied_patch: str | None = None) -> None:
    """Refiner가 고칠 파일의 현재 내용을 state에 담는다.

    통과한 검증에는 고칠 것이 없으므로 clone하지 않는다.
    """
    result = state.execution_result
    if result is None or state.status != JobStatus.failed:
        return
    paths = source_context.extract_paths(result.log)
    if state.judge_report.get("reason_category") == "dependency_install_failed" or not paths:
        # pip은 패키지 이름만 말한다. 고칠 파일은 매니페스트이므로 직접 붙인다.
        paths = list(source_context.MANIFEST_CANDIDATES) + [p for p in paths if p not in source_context.MANIFEST_CANDIDATES]
    if not paths:
        return
    state.source_files = source_context.collect(
        state.repository_url,
        paths,
        branch=state.branch,
        applied_patch=applied_patch or state.metrics.get("applied_patch"),
        clone_timeout_seconds=get_settings().repository_clone_timeout_seconds,
    )
    if state.source_files:
        state.events.append(f"Refiner: source files attached ({', '.join(state.source_files)})")
    else:
        # 파일을 못 읽으면 Refiner는 diff를 쓰지 못한다. 지어내는 것보다 남기는 것이 낫다.
        state.events.append(f"Refiner: source files unavailable for {', '.join(paths)}")


def _patch_is_applicable(state: AgentState) -> bool:
    """패치가 있고 실제 레포에 적용되는 것까지 확인됐는지."""
    if not state.refiner_report.get("patch_diff"):
        return False
    return bool((state.metrics.get("patch_check") or {}).get("applies"))


def _rerun_with_patch(state: AgentState, diff: str) -> tuple[SandboxResult, dict[str, object]]:
    """패치를 적용한 상태로 sandbox를 다시 돌리고 결과를 요약한다."""
    result = sandbox_runner.run_repository(
        state.preflight_report.repository_url,
        branch=state.branch,
        commit_sha=state.requested_commit_sha,
        patch_diff=diff,
    )
    summary: dict[str, object] = {
        "attempted": True,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "infra_error": result.infra_error,
        # 패치 적용 자체가 실패한 경우는 sandbox 스크립트가 전용 종료 코드로 알려준다.
        "patch_applied": result.exit_code != PATCH_APPLY_EXIT_CODE,
        "passed": result.exit_code == 0 and not result.timed_out,
    }
    if result.infra_error:
        state.events.append(f"Refiner: patch rerun unavailable: {result.infra_error}")
    elif summary["passed"]:
        state.events.append("Refiner: sandbox rerun with the patch passed")
    else:
        state.events.append(f"Refiner: sandbox rerun with the patch failed (exit={result.exit_code})")
    return result, summary


def _run_patch_rounds(state: AgentState, emit_progress) -> None:
    """패치를 적용해 재실행하고, 여전히 실패하면 그 결과를 근거로 다음 패치를 만든다.

    라운드마다 새 패치를 누적 diff 뒤에 이어 붙인다. 한 번에 다 적용하므로 같은 파일을 두 번
    고치는 것도 순서대로 적용된다. 멈추는 조건은 네 가지다. 재실행 통과, 라운드 상한
    (max_self_healing_retries), 누적 diff 상한(inspect_diff의 1MB), 그리고 더 만들 패치가 없음.

    재실행이 통과해도 최종 판정은 바꾸지 않는다. 제출된 레포는 여전히 실패했고, 패치는 제안이다.
    통과한 패치는 "이 변경이면 고쳐진다"는 증거로 남는다.
    """
    max_rounds = get_settings().max_self_healing_retries
    cumulative = str(state.refiner_report["patch_diff"])
    rounds: list[dict[str, object]] = []
    state.metrics["patch_rounds"] = rounds

    for round_ in range(1, max_rounds + 1):
        emit_progress(event_builder.REFINING, round_=round_, max_rounds=max_rounds)
        result, summary = _rerun_with_patch(state, cumulative)
        record: dict[str, object] = {
            "round": round_,
            "patch_bytes": len(cumulative.encode("utf-8")),
            **summary,
        }
        rounds.append(record)
        state.metrics["patch_rerun"] = summary
        if summary["passed"] or summary["infra_error"] or not summary["patch_applied"]:
            break
        if round_ == max_rounds:
            record["stopped"] = "round_limit"
            break

        follow_up, verdict = _next_patch_from_rerun(state, result, summary, cumulative)
        record["rerun_verdict"] = verdict
        if not follow_up:
            record["stopped"] = "no_further_patch"
            break
        merged = cumulative + ("" if cumulative.endswith("\n") else "\n") + follow_up
        gate = inspect_diff(merged)
        if not gate.accepted:
            record["stopped"] = gate.reason_code
            break
        applies = _check_applies(state, merged)
        if applies is None or not applies.accepted:
            record["stopped"] = applies.reason_code if applies else "patch_check_skipped"
            break
        cumulative = merged

    # 사람이 적용할 것은 누적 diff 전체다.
    state.refiner_report["patch_diff"] = cumulative


def _next_patch_from_rerun(
    state: AgentState, result: SandboxResult, summary: dict[str, object], applied_patch: str
) -> tuple[str | None, dict[str, object]]:
    """패치 적용 후에도 실패한 실행을 다시 판정하고, 그 근거로 다음 패치를 만든다.

    원본 판정을 덮으면 안 된다. 제출된 레포에 대한 판정이 최종 산출물이기 때문에
    복사한 state에서 돌리고 결과만 가져온다.
    """
    probe = state.model_copy(deep=True)
    probe.execution_result = result
    probe.metrics = _metrics_from_execution(probe)
    probe.sre_metrics = _sre_metrics_from_execution(probe)
    probe.metrics["patch_rerun"] = dict(summary)
    # 다음 패치는 이 패치 위에 적용된다. 무엇이 이미 적용됐는지 보여주지 않으면
    # 원본 파일 기준으로 패치를 써서 충돌한다.
    probe.metrics["applied_patch"] = applied_patch
    probe.judge_report = {}
    probe.source_files = {}
    probe.critic_feedback = {}
    probe.refiner_report = {}

    probe = judge_node(probe)
    attach_source_files(probe, applied_patch=applied_patch)
    verdict = {
        "status": probe.judge_report.get("status"),
        "reason_category": probe.judge_report.get("reason_category"),
    }
    if probe.judge_report.get("status") == "Pass":
        # 재실행은 실패로 끝났는데 규칙은 통과라고 본다면 근거가 어긋난 것이다. 더 고치지 않는다.
        return None, verdict
    probe = critic_node(probe)
    probe = refiner_node(probe)
    follow_up = probe.refiner_report.get("patch_diff")
    state.events.extend(probe.events[len(state.events) :])
    return (str(follow_up) if follow_up else None), verdict


def _check_applies(state: AgentState, diff: str) -> PatchVerdict | None:
    """레포를 얕게 clone해 패치 적용 가능성을 확인한다. 확인 자체를 못 하면 None."""
    workdir = tempfile.mkdtemp(prefix="codereferee-patchcheck-")
    try:
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", *(["--branch", state.branch] if state.branch else []),
             state.repository_url, workdir],
            capture_output=True, text=True, timeout=get_settings().repository_clone_timeout_seconds,
        )
        if clone.returncode != 0:
            state.events.append("Refiner: patch apply check skipped, clone failed")
            return None
        return check_applies(diff, Path(workdir))
    except (subprocess.TimeoutExpired, OSError) as exc:
        state.events.append(f"Refiner: patch apply check skipped: {exc.__class__.__name__}")
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _record_sqlite_artifacts(state: AgentState) -> None:
    try:
        record_validation_artifacts(state)
        state.events.append("SQLite: validation and patch suggestion artifacts recorded")
    except Exception as exc:  # pragma: no cover - storage failure should not mask validation result
        state.events.append(f"SQLite: artifact recording skipped: {exc.__class__.__name__}")
