from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

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
    parser.add_argument(
        "--results-dir",
        default=".codereferee/jobs",
        help="Directory where processed job JSON snapshots are written.",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)

    while True:
        state = process_next_repository_validation(block=True, timeout=args.block_timeout)
        if state is None:
            if args.once:
                print("No queued repository validation job before BLPOP timeout.")
                return
            continue

        result_path = _write_result_snapshot(state, results_dir)
        print(_format_processed_job(state, result_path))
        if args.once:
            return


def _write_result_snapshot(state: AgentState, results_dir: Path) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{state.job_id}.json"
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    return path


def _format_processed_job(state: AgentState, result_path: Path) -> str:
    lines = [f"Processed repository validation job {state.job_id}: {state.status}"]
    lines.append(f"Result snapshot: {result_path}")
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
