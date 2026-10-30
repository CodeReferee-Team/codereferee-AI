from pathlib import Path
import threading
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, TypedDict
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from prometheus_client import Counter, Histogram

from app.agents import source_context
from app.agents.nodes import chaos_aborted, chaos_recovered, chaos_target_configuration, critic_node, judge_node, planner_node, refiner_node, judgement_unavailable
from app.report import build_pdf_report, report_from_state
from app.models import (
    DEFAULT_SLO,
    AgentState,
    JobStatus,
    RepositoryValidationRequest,
    RepositoryValidationResponse,
    SandboxResult,
    SREMetrics,
)
from app.metrics import observation, prometheus
from app.config import get_settings
from app import events as event_builder
from app.queue.redis_queue import redis_task_queue
from app.repository.preflight import repository_preflight_runner
from app.sandbox.docker_runner import sandbox_runner
from app.storage.sqlite_store import job_store, record_validation_artifacts

VALIDATION_COUNTER = Counter("codereferee_repository_validations_total", "Total repository validations", ["status"])
SANDBOX_DURATION = Histogram("codereferee_repository_sandbox_duration_ms", "Repository sandbox duration in ms")
REPOSITORY_VALIDATION_TASK = "repository_validation"
# 카오스 응답임을 알리는 schema_version. chaos-v1에서 litmus-v1로 늘었고 앞으로도 늘 수 있다.
CHAOS_SCHEMA_PREFIXES = ("chaos-", "litmus-")


def create_validation_state(request: RepositoryValidationRequest, job_id: str | None = None) -> AgentState:
    return AgentState(
        job_id=job_id or str(uuid4()),
        request_id=request.request_id,
        repository_url=str(request.repository_url),
        branch=request.branch,
        requested_commit_sha=request.commit_sha,
        chaos_mode=request.chaos_mode,
        deployment_profile=request.deployment_profile,
        target_users=request.target_users,
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


@contextmanager
def _sandbox_heartbeat(emit_progress, step: str, *, interval: float = 20.0,
                       detail: str = "샌드박스 실행 중 (clone/build/배포/장애주입)"):
    """긴 샌드박스 호출 동안 progress를 주기적으로 재방출한다.

    layer-2는 샌드박스 안에서 수 분 걸리는데 그 사이 이벤트가 없으면 서버 stale
    스위퍼가 진행 중 작업을 '갱신 없음'으로 보고 ERROR로 확정한다(running-timeout).
    주기적 재방출로 updated_at을 살려 두고, 프론트가 멈춘 듯 보이지 않게 한다.
    heartbeat 실패는 본 실행을 절대 깨뜨리지 않는다(best-effort).
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(interval):
            try:
                emit_progress(step, detail=detail)
            except Exception:  # noqa: BLE001 - heartbeat는 본 실행을 깨면 안 된다
                pass

    thread = threading.Thread(target=_beat, name="sandbox-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)


def run_repository_validation(request: RepositoryValidationRequest, job_id: str | None = None) -> AgentState:
    """Run validation synchronously, bypassing Redis. Useful for local smoke tests."""
    state = create_validation_state(request, job_id)
    state.events.append("Input: GitHub repository URL received")
    return execute_repository_validation(state)


def execute_repository_validation(state: AgentState, output_queue=redis_task_queue) -> AgentState:
    """검증 한 건을 LangGraph StateGraph로 실행한다. 노드와 간선은 아래 '그래프' 절에 있다."""

    def emit_progress(step: str, **kwargs) -> None:
        # request_id가 없으면 서버발 요청이 아니다(로컬 동기 호출). 보낼 곳이 없다.
        if state.request_id:
            output_queue.publish(event_builder.progress_event(state, step, **kwargs))

    final = validation_graph().invoke(
        {"agent": state},
        config=_graph_config(emit_progress, output_queue=output_queue),
    )
    return final["agent"]


def _generate_report_pdf(state: AgentState) -> None:
    """검증마다 사람용 PDF 추천 리포트를 만들고 경로를 state에 남긴다.

    리포트 생성 실패(폰트 누락·렌더 오류)가 검증 결과를 망치면 안 된다. 예외는 삼키고
    이벤트만 남긴다 — PDF는 결과의 부가물이지 판정의 일부가 아니다.
    """
    try:
        out = Path(get_settings().report_output_dir) / f"{state.job_id}.pdf"
        build_pdf_report(report_from_state(state), out)
        state.report_pdf_path = str(out)
        state.events.append(f"Report: recommendation PDF written to {out}")
    except Exception as exc:  # noqa: BLE001 - 리포트 실패는 판정을 막지 않는다
        state.events.append(f"Report: PDF generation skipped: {type(exc).__name__}: {exc}")


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
        report_pdf_path=state.report_pdf_path,
    )


def _queue_payload(state: AgentState) -> dict[str, str | int | None]:
    # Match the server Redis schema so POST /jobs and server-originated tasks are interchangeable.
    return {
        "taskId": state.job_id,
        "repositoryUrl": state.repository_url,
        "branch": state.branch,
        "commitSha": state.requested_commit_sha,
        "chaosMode": state.chaos_mode,
        "deploymentProfile": state.deployment_profile,
        "targetUsers": state.target_users,
        "submittedAt": None,
    }


def _state_from_queue_payload(payload: dict) -> AgentState:
    if "repositoryUrl" in payload or "taskId" in payload:
        job_id = payload.get("taskId")
        repository_url = payload.get("repositoryUrl")
        branch = payload.get("branch")
        commit_sha = payload.get("commitSha")
        chaos_mode = payload.get("chaosMode")
        deployment_profile = payload.get("deploymentProfile")
        target_users = payload.get("targetUsers")
        request_id = payload.get("taskId")
        source = "server"
    elif payload.get("type") == REPOSITORY_VALIDATION_TASK:
        job_id = payload.get("job_id")
        repository_url = payload.get("repository_url")
        branch = payload.get("branch")
        commit_sha = payload.get("commit_sha")
        chaos_mode = payload.get("chaos_mode")
        deployment_profile = payload.get("deployment_profile")
        target_users = payload.get("target_users")
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
        chaos_mode=chaos_mode,
        deployment_profile=deployment_profile,
        target_users=target_users,
    )
    state = create_validation_state(request, job_id=job_id)
    if submitted_at := payload.get("submittedAt"):
        state.events.append(f"Queue: submittedAt={submitted_at}")
    state.events.append(f"Queue: payload schema={source}")
    return state


def _preflight_passed(report) -> bool:
    return bool(report and report.cloneable and report.executable)


# pytest는 수집 단계에서 깨지면 2로, 테스트가 실패하면 1로 끝낸다. 수집 오류는 대개 테스트
# 전용 의존성이 없는 것이고, 고칠 자리는 의존성 선언인데 로그는 테스트 파일 경로만 말한다.
# 매니페스트를 함께 주지 않으면 모델이 가진 파일은 테스트 파일뿐이라 그것을 고치려 한다.
# 실측으로 겪었다(pallets/itsdangerous, freezegun 없음).
_COLLECTION_ERROR_EXIT_CODE = 2
_MANIFEST_CONTEXT_CATEGORIES = ("dependency_install_failed", "no_manifest_detected")


def _needs_manifest_context(state: AgentState) -> bool:
    if state.judge_report.get("reason_category") in _MANIFEST_CONTEXT_CATEGORIES:
        return True
    result = state.execution_result
    return bool(result and result.exit_code == _COLLECTION_ERROR_EXIT_CODE)


def _dependency_clients(observation: dict[str, Any]) -> tuple[str, ...]:
    """장애를 건 의존성의 클라이언트 모듈. 묶음 실행이면 시나리오마다 본다."""
    observations = [observation] + [
        scenario.get("chaos_observation") or {}
        for scenario in observation.get("scenarios") or []
        if isinstance(scenario, dict)
    ]
    clients: list[str] = []
    for item in observations:
        if isinstance(item, dict):
            clients.extend(source_context.DEPENDENCY_CLIENTS.get(str(item.get("type")), ()))
    return tuple(dict.fromkeys(clients))


# 레포에 검증 설정이 없을 때 Refiner에 보여주는 빈 틀. 샌드박스는 replicas만 적은 파일을 자동 감지 위에
# 덮어쓰므로(codereferee-sandbox docs/repository-execution-plans.md) 이 한 줄만 바꾸면 복제본이 는다.
# spring-petclinic 부하 200명 Fail에서 Critic이 "복제본 1개"를 짚고도 고칠 파일이 없어 패치가 null이었다.
NEW_REPLICA_OVERRIDE = "version: 1\nservice:\n  replicas: 1\n"


def attach_source_files(state: AgentState, applied_patch: str | None = None) -> None:
    """Refiner가 고칠 파일의 현재 내용을 state에 담는다.

    통과한 검증에는 고칠 것이 없으므로 clone하지 않는다.
    """
    result = state.execution_result
    if result is None or state.status != JobStatus.failed:
        return
    paths = source_context.extract_paths(result.log)
    clients = _dependency_clients(result.chaos_observation)
    if result.chaos_observation:
        paths = list(source_context.CHAOS_CONFIG_CANDIDATES) + paths
    elif _needs_manifest_context(state) or not paths:
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
        dependency_clients=clients,
    )
    _offer_replica_override(state)
    if state.source_files:
        state.events.append(f"Refiner: source files attached ({', '.join(state.source_files)})")
    else:
        # 파일을 못 읽으면 Refiner는 diff를 쓰지 못한다. 지어내는 것보다 남기는 것이 낫다.
        state.events.append(f"Refiner: source files unavailable for {', '.join(paths)}")
def _offer_replica_override(state: AgentState) -> None:
    """복제본 1개로 카오스·부하에서 떨어졌는데 검증 설정이 없으면 새 파일 틀을 보여준다.

    이미 있으면 그 파일을 고치게 둔다. clone이 실패해 있는 파일을 못 읽었을 수도 있는데,
    그때 만든 생성 diff는 git apply --check에서 걸러진다.
    """
    state.metrics.pop("new_source_files", None)
    observation = state.execution_result.chaos_observation if state.execution_result else None
    if not observation or any(path in state.source_files for path in source_context.CHAOS_CONFIG_CANDIDATES):
        return
    if chaos_target_configuration(observation).get("replicas") != 1:
        return
    path = source_context.CHAOS_CONFIG_CANDIDATES[0]
    state.source_files = {**state.source_files, path: NEW_REPLICA_OVERRIDE}
    state.metrics["new_source_files"] = [path]


def _sandbox_contract_options(state: AgentState) -> dict[str, str | int]:
    """Pass optional external-Sandbox deployment context without changing legacy calls."""
    options: dict[str, str | int] = {}
    if state.chaos_mode:
        options["chaos_mode"] = state.chaos_mode
    if state.deployment_profile:
        options["deployment_profile"] = state.deployment_profile
    if state.target_users:
        options["target_users"] = state.target_users
    # requestId is the cross-service correlation key.  It must accompany
    # ordinary validations as well as profile-driven Chaos runs.
    if state.request_id:
        options["request_id"] = state.request_id
    return options


def _patch_diff_from(refiner_report: dict[str, Any]) -> str | None:
    """Refiner가 낸 누적 diff. 결정적 fallback은 diff를 만들 수 없어 None이다."""
    diff = (refiner_report or {}).get("patch_diff")
    if not isinstance(diff, str) or not diff.strip():
        return None
    return diff


def _join_patches(applied: str | None, diff: str) -> str:
    """앞서 적용한 패치 뒤에 새 diff를 잇는다.

    두 번째 라운드부터 Refiner는 앞 패치가 적용된 파일을 보고 diff를 쓰므로 그 diff는 제출된
    커밋에 단독으로 적용되지 않는다. git apply는 한 입력 안의 diff를 순서대로 적용하므로
    이어 붙이면 base commit 대비 누적 패치가 된다.
    """
    if not applied:
        return diff
    separator = "" if applied.endswith("\n") else "\n"
    return applied + separator + diff


def _patch_not_applied(result: SandboxResult) -> bool:
    """샌드박스가 패치 단계에서 멈췄는가. 레포를 실행해보지 못했으므로 판정할 결과가 없다."""
    return (result.sandbox_report or {}).get("failed_step") == "patch"


def _judge_status(state: AgentState) -> str | None:
    value = (state.judge_report or {}).get("status")
    return str(value) if value else None


def _round_record(
    round_number: int,
    patch_diff: str,
    before_status: str | None,
    after_status: str | None,
    result: SandboxResult,
) -> dict[str, Any]:
    return {
        "round": round_number,
        "patch_bytes": len(patch_diff.encode("utf-8")),
        "before_judge_status": before_status,
        "after_judge_status": after_status,
        "sandbox_exit_code": result.exit_code,
        "failed_step": (result.sandbox_report or {}).get("failed_step"),
    }


def _run_refinement_rounds(state: AgentState, emit_progress) -> AgentState:
    """재검증 루프만 따로 돌린다. 본 그래프에서는 같은 서브그래프가 'refine' 노드로 붙는다.

    evals/repair.py가 운영과 같은 루프를 태우려고 이 진입점을 쓴다.
    """
    final = refinement_graph().invoke({"agent": state}, config=_graph_config(emit_progress))
    return final["agent"]


def _restore_submitted_verdict(state: AgentState, submitted: dict[str, object]) -> AgentState:
    """라운드가 덮어쓴 판정을 제출 시점 값으로 되돌린다.

    패치를 적용해 통과한 것은 "이 변경이면 고쳐진다"는 증거이고, 제출된 코드가 통과한 것이 아니다.
    심사 서비스가 "우리가 고쳐줬으니 합격"이라고 내보내면 심사가 아니게 된다.
    검증된 패치는 refiner_report에, 라운드별 기록은 refine_rounds에 남는다.
    """
    verified = any(record.get("after_judge_status") == "Pass" for record in state.refine_rounds)
    state.judge_report = submitted["judge_report"]
    state.status = submitted["status"]
    state.execution_result = submitted["execution_result"]
    state.metrics = submitted["metrics"]
    state.sre_metrics = submitted["sre_metrics"]
    state.metrics["patch_verified"] = verified
    if verified:
        state.events.append(
            "Refine: a patch made the sandbox pass; the verdict for the submitted commit is unchanged"
        )
    return state


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
        # 카오스를 요청했는데 관측이 없으면 판정 근거가 없다. 이 경우를 통과로 보내면
        # 실험이 돌지 않았는데 합격했다고 보고하게 된다.
        if str(result.schema_version or "").startswith(CHAOS_SCHEMA_PREFIXES):
            return "chaos_evidence_missing"
        return None
    if chaos_aborted(observation) or result.source.get("aborted"):
        return "chaos_experiment_aborted"
    if not result.baseline or chaos_recovered(observation) is None:
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
    if result.observation_status:
        metrics["observation_status"] = result.observation_status
    if result.infra_error:
        metrics["infra_error"] = result.infra_error
        # 판정 불가 화면이 "어디서, 왜"를 말할 수 있게 멈춘 단계와 마지막 오류 줄을 남긴다.
        if result.stopped_at:
            metrics["stopped_at"] = result.stopped_at
        lines = result.stderr.strip().splitlines()
        if lines:
            metrics["stopped_reason"] = lines[-1][:300]
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


def _record_sqlite_artifacts(state: AgentState) -> None:
    try:
        record_validation_artifacts(state)
        state.events.append("SQLite: validation and patch suggestion artifacts recorded")
    except Exception as exc:  # pragma: no cover - storage failure should not mask validation result
        state.events.append(f"SQLite: artifact recording skipped: {exc.__class__.__name__}")


# ---------------------------------------------------------------------------
# 그래프
#
# 파이프라인을 LangGraph StateGraph로 엮는다. 노드는 파이프라인의 한 단계이고, 분기는 전부
# 결정적 규칙이다(어느 간선을 탈지 LLM이 정하지 않는다). LLM은 planner·critic·refiner 노드
# 안에서 서술과 편집만 만든다.
#
#   preflight → planner ─┬─ sandbox ──────┬→ infra_gate ─┬→ judge ─┬→ critic → refiner → refine → finalize
#                        └─ skip_sandbox ─┘              └────────┴→ finalize (판정 불가)
#
# refine은 재검증 루프 서브그래프다.
#
#   refine_start → refine_prepare → refine_rerun → refine_judge → refine_regenerate ─┐
#                       ↑                                                            │
#                       └──────────────────── 라운드가 남았으면 ──────────────────────┘
#   멈출 사유가 생기면 refine_restore로, 라운드를 다 쓰면 refine_exhausted → refine_restore로 간다.
#
# 노드 안에서는 judge_node·sandbox_runner 같은 이름을 이 모듈 전역에서 찾는다. 테스트가
# 모듈 속성을 바꿔 끼우면 그래프도 바뀐 것을 부른다.
# ---------------------------------------------------------------------------


class ValidationGraphState(TypedDict, total=False):
    agent: AgentState
    # 판정을 건너뛸 사유. 있으면 Critic/Refiner 없이 finalize로 간다.
    halt: str | None
    # 재검증 루프 상태. _run_refinement_rounds의 지역 변수였던 것들이다.
    refine_skipped: bool
    refine_stop: bool
    submitted: dict[str, Any]
    applied: str | None
    pending_patch: str | None
    before_status: str | None
    round: int
    max_rounds: int


def _graph_config(emit_progress, *, output_queue=None) -> dict[str, Any]:
    # 라운드 하나가 노드 다섯 개를 지난다. 기본 recursion_limit(25)로는 라운드를 늘린 설정에서
    # 예외로 끝나 결과 이벤트가 나가지 않는다.
    rounds = max(0, get_settings().max_self_healing_retries)
    return {
        "configurable": {"emit_progress": emit_progress, "output_queue": output_queue},
        "recursion_limit": 30 + 6 * rounds,
    }


def _emitter(config: RunnableConfig):
    return config["configurable"]["emit_progress"]


def _preflight_step(graph_state: ValidationGraphState, config: RunnableConfig) -> ValidationGraphState:
    state = graph_state["agent"]
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
    _emitter(config)(event_builder.PREFLIGHT)
    state.events.append(f"Preflight: {'passed' if _preflight_passed(state.preflight_report) else 'failed'}")
    return {"agent": state}


def _planner_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    return {"agent": planner_node(graph_state["agent"])}


def _route_after_planner(graph_state: ValidationGraphState) -> str:
    return "sandbox" if _preflight_passed(graph_state["agent"].preflight_report) else "skip_sandbox"


def _sandbox_step(graph_state: ValidationGraphState, config: RunnableConfig) -> ValidationGraphState:
    state = graph_state["agent"]
    emit_progress = _emitter(config)
    state.events.append("Sandbox: repository clone and smoke validation started")
    emit_progress(event_builder.BASELINE)
    # layer-2는 샌드박스 안에서 수 분 걸린다. 그 동안 heartbeat로 progress를 살려
    # 두지 않으면 서버 stale 스위퍼가 진행 중 작업을 ERROR로 확정한다.
    with _sandbox_heartbeat(emit_progress, event_builder.BASELINE):
        state.execution_result = sandbox_runner.run_repository(
            state.preflight_report.repository_url,
            branch=state.branch,
            commit_sha=state.requested_commit_sha,
            **_sandbox_contract_options(state),
        )
    _fill_resource_metrics(state.execution_result, state.request_id)
    state.metrics = _metrics_from_execution(state)
    state.sre_metrics = _sre_metrics_from_execution(state)
    SANDBOX_DURATION.observe(state.execution_result.duration_ms)
    state.events.append("Sandbox: repository clone and smoke validation finished")
    if state.execution_result.chaos_observation:
        # 카오스는 sandbox 안에서 일어나므로 실시간 보고가 불가능하다.
        # 실제로 실험이 있었을 때만 사후에 알린다. 없었다면 보내지 않는다.
        emit_progress(event_builder.CHAOS, detail=str(state.execution_result.chaos_observation.get("type")))
    return {"agent": state}


def _fill_resource_metrics(result: SandboxResult, request_id: str | None) -> None:
    """cpu/memory는 샌드박스가 요약값 없이 null로 보낸다.

    먼저 응답에 실린 1초 시계열(source.metrics_observation)에서 장애 구간 최대치를 낸다.
    그래도 비어 있으면 바깥 Prometheus에 request_id로 묻는다. 연동 전(설정 없음)이면 no-op.
    """
    observation.fill_resource_metrics(
        result.metrics, (result.source or {}).get("metrics_observation"), result.chaos_observation
    )
    prometheus.enrich_resource_metrics(result.metrics, request_id, result.chaos_observation, get_settings())


def _skip_sandbox_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    state = graph_state["agent"]
    state.events.append("Sandbox: skipped because preflight failed")
    state.metrics = _metrics_from_execution(state)
    state.sre_metrics = _sre_metrics_from_execution(state)
    return {"agent": state}


def _infra_gate_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    state = graph_state["agent"]
    infra_error = _infra_error_reason(state)
    if infra_error:
        # 인프라 문제로 판정 자체가 불가능하다. Judge/Critic/Refiner를 건너뛴다.
        # 멀쩡한 사용자 코드를 두고 Critic이 고칠 곳을 찾게 두면 안 되기 때문이다.
        state.status = JobStatus.error
        state.events.append(f"Workflow: infra error, judgement skipped: {infra_error}")
    return {"agent": state, "halt": infra_error}


def _judge_step(graph_state: ValidationGraphState, config: RunnableConfig) -> ValidationGraphState:
    _emitter(config)(event_builder.JUDGING)
    state = judge_node(graph_state["agent"])
    unavailable = judgement_unavailable(state)
    if unavailable:
        # 환경 한계와 준비 단계 실패는 "안정성을 재지 못한 것"이다(판정 불가 = ERROR).
        # FAILED로 두면 화면이 불합격이라 말하고, Critic이 안정성과 무관한 곳을 고치려 든다.
        # infra_error와 같은 이유로 Critic/Refiner를 건너뛴다(judge-policy 3.0절).
        state.status = JobStatus.error
        state.events.append(f"Workflow: judgement unavailable ({unavailable}), stability not measured")
    return {"agent": state, "halt": unavailable}


def _route_unless_halted(next_node: str):
    def route(graph_state: ValidationGraphState) -> str:
        return "finalize" if graph_state.get("halt") else next_node

    return route


def _critic_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    state = graph_state["agent"]
    attach_source_files(state)
    return {"agent": critic_node(state)}


def _refiner_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    return {"agent": refiner_node(graph_state["agent"])}


def _finalize_step(graph_state: ValidationGraphState, config: RunnableConfig) -> ValidationGraphState:
    state = graph_state["agent"]
    VALIDATION_COUNTER.labels(status=state.status).inc()
    _generate_report_pdf(state)
    _record_sqlite_artifacts(state)
    job_store.save(state)
    output_queue = config["configurable"].get("output_queue")
    if state.request_id and output_queue is not None:
        # 종료 이벤트는 정확히 한 번. 이 뒤로는 어떤 progress도 보내지 않는다.
        output_queue.publish(event_builder.result_event(state))
    return {"agent": state}


# --- 재검증 루프 ------------------------------------------------------------
#
# Judge가 실패로 본 경우 Refiner의 diff를 적용해 재검증을 반복한다.
# 유저 레포에는 절대 push하지 않는다. diff는 샌드박스 안에서만 적용되고
# 최종 산출물은 "검증된 diff"로 리포트에 담긴다.
#
# diff가 없으면 즉시 멈춘다. 고칠 수단이 없는데 반복해봐야 같은 결과이기 때문이며,
# LLM이 설정되지 않은 환경에서는 이 경로로 빠져 기존 동작이 그대로 유지된다.


def _refine_start_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    state = graph_state["agent"]
    max_rounds = get_settings().max_self_healing_retries
    if max_rounds <= 0 or state.preflight_report is None:
        return {"refine_skipped": True}
    # 제출된 레포에 대한 판정이 최종 산출물이다. 라운드에서 패치를 적용해 통과했다고
    # 제출된 코드가 통과한 것은 아니므로, 판정과 실행 결과를 되돌릴 수 있게 보관한다.
    submitted = {
        "judge_report": dict(state.judge_report),
        "status": state.status,
        "execution_result": state.execution_result,
        "metrics": dict(state.metrics),
        "sre_metrics": state.sre_metrics,
    }
    # applied: 샌드박스에서 적용된 누적 패치. Refiner가 다음 diff를 쓸 때 보는 파일도 이것이 적용된 상태다.
    return {"refine_skipped": False, "submitted": submitted, "applied": None, "round": 1, "max_rounds": max_rounds}


def _route_refine_start(graph_state: ValidationGraphState) -> str:
    return END if graph_state.get("refine_skipped") else "refine_prepare"


def _refine_prepare_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    """라운드 시작. 실패 판정이고, diff가 있고, 상한 안이면 재실행할 누적 패치를 만든다."""
    state = graph_state["agent"]
    if state.status != JobStatus.failed:
        return {"refine_stop": True}

    new_diff = _patch_diff_from(state.refiner_report)
    if not new_diff:
        state.events.append("Refine: no patch diff produced, refinement loop stopped")
        return {"refine_stop": True}
    patch_diff = _join_patches(graph_state.get("applied"), new_diff)
    state.refiner_report["patch_diff"] = patch_diff
    patch_bytes = len(patch_diff.encode("utf-8"))
    if patch_bytes > get_settings().max_patch_diff_bytes:
        # 수정 범위가 이 정도면 판정을 신뢰하기 어렵다.
        state.events.append(f"Refine: patch diff too large ({patch_bytes} bytes), refinement loop stopped")
        return {"refine_stop": True}
    return {"refine_stop": False, "pending_patch": patch_diff, "before_status": _judge_status(state)}


def _refine_rerun_step(graph_state: ValidationGraphState, config: RunnableConfig) -> ValidationGraphState:
    """누적 패치를 얹어 샌드박스를 다시 돌린다."""
    state = graph_state["agent"]
    round_number, max_rounds = graph_state["round"], graph_state["max_rounds"]
    patch_diff, before_status = graph_state["pending_patch"], graph_state.get("before_status")
    _emitter(config)(event_builder.REFINING, round_=round_number, max_rounds=max_rounds)
    state.events.append(f"Refine: round {round_number}/{max_rounds} re-validating with patch")

    result = sandbox_runner.run_repository(
        state.preflight_report.repository_url,
        branch=state.branch,
        commit_sha=state.requested_commit_sha,
        patch_diff=patch_diff,
        **_sandbox_contract_options(state),
    )
    if _patch_not_applied(result):
        # 실행해보지 못한 라운드의 결과로 Critic과 Refiner를 다시 돌리면 리포트가 레포의 안정성
        # 대신 "패치가 어긋났다"를 설명한다. 실서버에서 겪었다(2026-10-23).
        state.events.append(
            f"Refine: round {round_number} patch did not apply in the sandbox, refinement loop stopped"
        )
        state.refine_rounds.append(_round_record(round_number, patch_diff, before_status, None, result))
        state.refiner_report["patch_diff"] = graph_state.get("applied")
        return {"refine_stop": True}

    state.execution_result = result
    _fill_resource_metrics(result, state.request_id)
    state.metrics = _metrics_from_execution(state)
    state.sre_metrics = _sre_metrics_from_execution(state)
    SANDBOX_DURATION.observe(result.duration_ms)

    infra_error = _infra_error_reason(state)
    if infra_error:
        state.status = JobStatus.error
        state.events.append(f"Refine: round {round_number} hit infra error, judgement skipped: {infra_error}")
        state.refine_rounds.append(_round_record(round_number, patch_diff, before_status, None, result))
        return {"refine_stop": True, "applied": patch_diff}
    return {"refine_stop": False, "applied": patch_diff}


def _refine_judge_step(graph_state: ValidationGraphState, config: RunnableConfig) -> ValidationGraphState:
    _emitter(config)(event_builder.JUDGING)
    state = judge_node(graph_state["agent"])
    round_number = graph_state["round"]
    state.refine_rounds.append(
        _round_record(round_number, graph_state["pending_patch"], graph_state.get("before_status"),
                      _judge_status(state), state.execution_result)
    )
    if state.status == JobStatus.success:
        state.events.append(f"Refine: round {round_number} passed after applying the patch")
        return {"agent": state, "refine_stop": True}
    return {"agent": state, "refine_stop": False}


def _refine_regenerate_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    """다음 라운드를 위해 패치가 적용된 파일로 Critic과 Refiner를 다시 돌린다."""
    state = graph_state["agent"]
    # 이전 라운드의 파일을 남겨두면 Refiner가 어느 상태를 기준으로 diff를 썼는지 알 수 없다.
    state.source_files = {}
    attach_source_files(state, applied_patch=graph_state.get("applied"))
    state = critic_node(state)
    state = refiner_node(state)
    return {"agent": state, "round": graph_state["round"] + 1}


def _refine_exhausted_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    # 라운드를 다 썼다. 마지막 Refiner의 diff는 실행되지 않고 수정안으로 남으므로
    # 그것도 제출된 커밋에 적용할 수 있는 누적 패치여야 한다.
    state = graph_state["agent"]
    if state.status == JobStatus.failed and (last := _patch_diff_from(state.refiner_report)):
        state.refiner_report["patch_diff"] = _join_patches(graph_state.get("applied"), last)
    return {"agent": state}


def _refine_restore_step(graph_state: ValidationGraphState) -> ValidationGraphState:
    return {"agent": _restore_submitted_verdict(graph_state["agent"], graph_state["submitted"])}


def _continue_or_restore(next_node: str):
    def route(graph_state: ValidationGraphState) -> str:
        return "refine_restore" if graph_state.get("refine_stop") else next_node

    return route


def _route_next_round(graph_state: ValidationGraphState) -> str:
    return "refine_exhausted" if graph_state["round"] > graph_state["max_rounds"] else "refine_prepare"


@lru_cache(maxsize=1)
def refinement_graph():
    builder = StateGraph(ValidationGraphState)
    builder.add_node("refine_start", _refine_start_step)
    builder.add_node("refine_prepare", _refine_prepare_step)
    builder.add_node("refine_rerun", _refine_rerun_step)
    builder.add_node("refine_judge", _refine_judge_step)
    builder.add_node("refine_regenerate", _refine_regenerate_step)
    builder.add_node("refine_exhausted", _refine_exhausted_step)
    builder.add_node("refine_restore", _refine_restore_step)

    builder.add_edge(START, "refine_start")
    builder.add_conditional_edges("refine_start", _route_refine_start, ["refine_prepare", END])
    builder.add_conditional_edges("refine_prepare", _continue_or_restore("refine_rerun"),
                                  ["refine_rerun", "refine_restore"])
    builder.add_conditional_edges("refine_rerun", _continue_or_restore("refine_judge"),
                                  ["refine_judge", "refine_restore"])
    builder.add_conditional_edges("refine_judge", _continue_or_restore("refine_regenerate"),
                                  ["refine_regenerate", "refine_restore"])
    builder.add_conditional_edges("refine_regenerate", _route_next_round, ["refine_prepare", "refine_exhausted"])
    builder.add_edge("refine_exhausted", "refine_restore")
    builder.add_edge("refine_restore", END)
    return builder.compile()


@lru_cache(maxsize=1)
def validation_graph():
    builder = StateGraph(ValidationGraphState)
    builder.add_node("preflight", _preflight_step)
    builder.add_node("planner", _planner_step)
    builder.add_node("sandbox", _sandbox_step)
    builder.add_node("skip_sandbox", _skip_sandbox_step)
    builder.add_node("infra_gate", _infra_gate_step)
    builder.add_node("judge", _judge_step)
    builder.add_node("critic", _critic_step)
    builder.add_node("refiner", _refiner_step)
    builder.add_node("refine", refinement_graph())
    builder.add_node("finalize", _finalize_step)

    builder.add_edge(START, "preflight")
    builder.add_edge("preflight", "planner")
    builder.add_conditional_edges("planner", _route_after_planner, ["sandbox", "skip_sandbox"])
    builder.add_edge("sandbox", "infra_gate")
    builder.add_edge("skip_sandbox", "infra_gate")
    builder.add_conditional_edges("infra_gate", _route_unless_halted("judge"), ["judge", "finalize"])
    builder.add_conditional_edges("judge", _route_unless_halted("critic"), ["critic", "finalize"])
    builder.add_edge("critic", "refiner")
    builder.add_edge("refiner", "refine")
    builder.add_edge("refine", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()
