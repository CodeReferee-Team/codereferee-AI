from __future__ import annotations

import argparse
import json
import os
from typing import Any

from prometheus_client import start_http_server

from app.models import AgentState
from app.workflow.repository_validation import process_next_repository_validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CodeReferee repository-validation Redis worker.")
    parser.add_argument("--once", action="store_true", help="Process one queued job and exit.")
    parser.add_argument(
        "--block-timeout",
        type=int,
        default=0,
        help="BLPOP timeout in seconds. 0 waits forever until a task arrives.",
    )
    args = parser.parse_args()

    # Prometheus가 워커를 scrape할 수 있게 연다. prometheus_client는 프로세스마다
    # 레지스트리를 따로 두기 때문에, FastAPI의 /metrics를 긁어도 이 루프에서 올린
    # 카운터는 보이지 않는다. 워커 안에서 직접 열어야 한다.
    # --once는 테스트와 수동 실행용이라 포트를 잡지 않는다.
    metrics_port = int(os.getenv("METRICS_PORT", "8000"))
    if not args.once and metrics_port:
        start_http_server(metrics_port)
        print(f"Worker metrics on :{metrics_port}/metrics")

    while True:
        state = process_next_repository_validation(block=True, timeout=args.block_timeout)
        if state is None:
            if args.once:
                print("No queued repository validation job before BLPOP timeout.")
                return
            continue

        print(_format_processed_job(state))
        if args.once:
            return


def _format_processed_job(state: AgentState) -> str:
    lines = [f"Processed repository validation job {state.job_id}: {state.status}"]
    lines.append(f"Stored in SQLite job store; fetch with GET /jobs/{state.job_id}")
    lines.append(f"Repository: {state.repository_url}")
    if state.branch:
        lines.append(f"Branch: {state.branch}")
    if state.resolved_commit_sha or state.requested_commit_sha:
        lines.append(f"Commit: {state.resolved_commit_sha or state.requested_commit_sha}")

    lines.append(f"Preflight: {_preflight_summary(state)}")
    if state.execution_result:
        lines.append(
            "Sandbox: "
            f"exit_code={state.execution_result.exit_code}, "
            f"timed_out={state.execution_result.timed_out}, "
            f"duration_ms={state.execution_result.duration_ms}, "
            f"server_started={state.execution_result.server_started}, "
            f"http_status={state.execution_result.http_status}, "
            f"browser_loaded={state.execution_result.browser_loaded}"
        )
        if state.execution_result.server_url:
            lines.append(f"Server URL: {state.execution_result.server_url}")
        if state.execution_result.page_title:
            lines.append(f"Page title: {state.execution_result.page_title}")

    if state.status != "success":
        lines.append(f"Failure reason: {_failure_reason(state)}")

    if state.events:
        lines.append("Events: " + " -> ".join(state.events[-8:]))
    return "\n".join(lines)


def _preflight_summary(state: AgentState) -> str:
    report = state.preflight_report
    if report is None:
        return "missing"
    return (
        f"cloneable={report.cloneable}, executable={report.executable}, "
        f"reason={report.reason or 'n/a'}"
    )


def _failure_reason(state: AgentState) -> str:
    judge_reason = _stringify(state.judge_report.get("reason")) if state.judge_report else ""
    if judge_reason:
        return _compact(judge_reason)
    if state.execution_result and state.execution_result.stderr:
        return _compact(state.execution_result.stderr)
    if state.preflight_report and state.preflight_report.reason:
        return _compact(state.preflight_report.reason)
    return "Unknown; inspect the result snapshot JSON."


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _compact(text: str, *, limit: int = 700) -> str:
    compacted = " ".join(text.split())
    if len(compacted) <= limit:
        return compacted
    return compacted[:limit] + "..."


if __name__ == "__main__":
    main()
