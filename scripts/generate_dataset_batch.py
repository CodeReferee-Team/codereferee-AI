#!/usr/bin/env python3
"""Generate a daily synthetic CodeReferee dataset batch.

The generator creates candidate evaluation/fine-tuning rows only. Every row is
marked as human-review-required and not real-execution-observed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROWS_PER_FILE = 200
FILES = {
    "preflight_failures.jsonl": "preflight.expected_reason_category",
    "sandbox_failures.jsonl": "sandbox.expected_failure_type",
    "metrics_judge_cases.jsonl": "metrics.expected_reason_category",
    "critic_refiner_cases.jsonl": "critic.failure_type",
    "local_sample_repo_specs.jsonl": "local_repo.stack",
}

PREFLIGHT_REASONS = [
    "repository_not_found",
    "branch_not_found",
    "commit_not_found",
    "invalid_url_format",
    "unsupported_host",
    "empty_repository_url",
    "private_repo_forbidden",
    "tree_url_requires_normalization",
    "blob_url_not_repository",
    "unsupported_scheme",
    "owner_only_url",
    "tag_not_found",
    "rate_limited",
    "network_dns_failure",
    "auth_required",
    "large_repo_policy_block",
    "archived_repo_policy_block",
    "submodule_only_repo",
    "git_lfs_required",
    "monorepo_subdir_required",
    "invalid_commit_sha_format",
    "redirect_not_allowed",
    "github_api_unavailable",
    "repo_name_contains_invalid_chars",
    "default_branch_missing",
]

SANDBOX_FAILURES = [
    "dependency_install_failed",
    "pytest_failure",
    "sandbox_timeout",
    "memory_limit_exceeded",
    "cpu_quota_exceeded",
    "missing_env",
    "entrypoint_import_error",
    "port_bind_failure",
    "gradle_permission_denied",
    "gradle_test_failed",
    "maven_permission_denied",
    "maven_test_failed",
    "npm_install_failed",
    "npm_test_missing",
    "docker_build_failed",
    "dockerfile_missing",
    "redis_unavailable",
    "postgres_unavailable",
    "unsupported_stack",
    "no_smoke_command",
    "permission_denied_runtime",
    "syntax_error",
    "package_lock_mismatch",
    "missing_settings_gradle",
    "docker_daemon_unavailable",
]

METRIC_REASONS = [
    "all_slo_passed",
    "latency_slo_violation",
    "error_rate_slo_violation",
    "cpu_saturation",
    "memory_pressure",
    "unexpected_restart",
    "availability_slo_violation",
    "multiple_slo_violations",
    "redis_connection_errors",
    "database_connection_errors",
    "no_traffic_observed",
    "missing_metrics",
    "boundary_pass",
    "latency_boundary_fail",
    "error_boundary_fail",
    "cpu_boundary_fail",
    "request_spike_degradation",
    "cold_start_latency",
    "stable_under_load",
    "near_limit_pass",
]

CRITIC_FAILURES = [
    "redis_connection_failure",
    "database_connection_failure",
    "external_api_timeout",
    "missing_health_check",
    "no_retry_policy",
    "no_circuit_breaker",
    "memory_leak_signal",
    "cpu_saturation",
    "log_observability_gap",
    "metrics_observability_gap",
    "graceful_shutdown_missing",
    "dependency_install_failed",
    "test_failure",
    "network_latency_vulnerability",
    "packet_loss_vulnerability",
    "no_traffic_observed",
    "unsupported_project_stack",
    "missing_environment_variable",
    "slow_startup",
    "multiple_slo_violations",
]

LOCAL_STACKS = [
    "python",
    "fastapi",
    "java-gradle",
    "java-maven",
    "node",
    "docker",
    "spring-boot",
    "redis-python",
    "postgres-spring",
    "worker-python",
]

STACK_FILES = {
    "python": ["requirements.txt", "app.py", "tests/test_app.py"],
    "fastapi": ["requirements.txt", "main.py", "tests/test_health.py"],
    "java-gradle": ["build.gradle", "settings.gradle", "gradlew", "src/test/java/AppTest.java"],
    "java-maven": ["pom.xml", "src/main/java/App.java", "src/test/java/AppTest.java"],
    "node": ["package.json", "src/index.js", "test/app.test.js"],
    "docker": ["Dockerfile", "compose.yaml", "app.py"],
    "spring-boot": ["build.gradle", "src/main/java/Application.java", "src/test/java/ApplicationTests.java"],
    "redis-python": ["requirements.txt", "worker.py", "tests/test_queue.py"],
    "postgres-spring": ["build.gradle", "src/main/resources/application.yml", "src/test/java/RepositoryTest.java"],
    "worker-python": ["requirements.txt", "worker.py", "tests/test_worker.py"],
}

CRITIC_LIBRARY = {
    "redis_connection_failure": ("Connection refused: redis:6379", "Redis dependency failure is not handled gracefully.", "Add timeout, retry, fallback, and health checks.", "medium"),
    "database_connection_failure": ("Connection refused: postgres:5432", "Database dependency is unavailable or misconfigured.", "Validate DB config and add startup/dependency health checks.", "medium"),
    "external_api_timeout": ("ReadTimeout from upstream API", "External API calls lack bounded timeout and fallback handling.", "Set explicit timeouts and safe fallback behavior.", "high"),
    "missing_health_check": ("No health endpoint detected", "The service lacks a reliable readiness or liveness check.", "Add health and readiness endpoints with dependency checks.", "medium"),
    "no_retry_policy": ("single attempt failed under transient network error", "Transient dependency failures are not retried safely.", "Add bounded retry with backoff and jitter.", "medium"),
    "no_circuit_breaker": ("upstream failure propagated to all requests", "The service does not isolate persistent upstream failure.", "Add circuit breaker or bulkhead behavior around the dependency.", "high"),
    "memory_leak_signal": ("memory usage increased across each sample window", "Memory usage grows without returning to baseline.", "Profile retained objects and add a regression load test.", "high"),
    "cpu_saturation": ("cpu usage remained above SLO threshold", "CPU saturation indicates inefficient processing or missing limits.", "Add profiling, limits, and backpressure.", "medium"),
    "log_observability_gap": ("failure occurred without structured error logs", "Logs do not expose enough evidence for diagnosis.", "Add structured logs with request and dependency context.", "low"),
    "metrics_observability_gap": ("required Prometheus metrics missing", "Metrics do not expose SLO signals.", "Expose latency, error, saturation, and dependency metrics.", "medium"),
    "graceful_shutdown_missing": ("process terminated while requests were in flight", "Shutdown does not drain active work safely.", "Handle SIGTERM and drain requests before exit.", "medium"),
    "dependency_install_failed": ("package installation failed", "Dependencies are not pinned or cannot be installed reproducibly.", "Pin dependencies and add lockfile validation.", "medium"),
    "test_failure": ("test suite exited with failure", "The repository fails its own deterministic checks.", "Fix failing tests before reliability validation.", "medium"),
    "network_latency_vulnerability": ("p95 latency spiked during delay injection", "The service is sensitive to network delay.", "Add timeout budgets, caching, and async boundaries.", "high"),
    "packet_loss_vulnerability": ("requests failed during packet loss injection", "The service lacks resilience under lossy network conditions.", "Add retry, idempotency, and connection pool tuning.", "high"),
    "no_traffic_observed": ("no requests observed during smoke run", "The validation did not exercise the service path.", "Add deterministic smoke traffic before judging SLOs.", "medium"),
    "unsupported_project_stack": ("no supported build or smoke command detected", "The sandbox cannot infer how to execute the repository.", "Document build/test/run commands for the sandbox.", "low"),
    "missing_environment_variable": ("required environment variable is absent", "Runtime configuration is not validated before startup.", "Add config validation and document required variables.", "low"),
    "slow_startup": ("service did not become ready before timeout", "Startup is too slow or readiness is not exposed.", "Optimize startup and expose readiness checks.", "medium"),
    "multiple_slo_violations": ("latency, errors, and restarts exceeded SLOs", "Multiple reliability signals failed at once.", "Prioritize dependency stability, resource limits, and observability.", "high"),
}


@dataclass(frozen=True)
class BatchContext:
    batch_date: str
    batch_id: str
    compact_date: str
    dataset_version: str


def common_source() -> dict[str, Any]:
    return {
        "type": "synthetic_generated_seed",
        "created_from": "daily_batch_programmatic_sre_case_generation",
        "llm_generated_allowed": True,
        "human_review_required": True,
        "real_execution_observed": False,
    }


def training_usage() -> dict[str, Any]:
    return {
        "suitable_for": ["evaluation", "fine_tuning_seed", "llm_expansion_seed"],
        "not_suitable_for": ["unreviewed_final_training"],
        "note": "Daily synthetic batch. Human review is required before fine-tuning.",
    }


def spaced(value: str) -> str:
    return value.replace("_", " ")


def cycle(values: list[str], total: int) -> list[str]:
    return [values[index % len(values)] for index in range(total)]


def preflight_row(ctx: BatchContext, index: int, reason: str) -> dict[str, Any]:
    branch = "main"
    commit_sha = None
    if reason == "branch_not_found":
        branch = f"missing-{index:03d}"
    if reason == "commit_not_found":
        commit_sha = "f" * 40
    return {
        "case_id": f"PREFLIGHT-{ctx.compact_date}-{index:03d}",
        "batch_id": ctx.batch_id,
        "dataset_version": ctx.dataset_version,
        "agent_target": "preflight",
        "repo_url": f"https://github.com/CodeReferee-Dataset/{reason}-{index:03d}",
        "branch": branch,
        "commit_sha": commit_sha,
        "input_type": reason,
        "expected_stage": "preflight",
        "expected_status": "Fail",
        "expected_reason_category": reason,
        "expected_cloneable": False,
        "expected_evidence_contains": [spaced(reason)],
        "source": common_source(),
        "training_usage": training_usage(),
    }


def sandbox_execution_for(failure: str, index: int) -> dict[str, Any]:
    stderr = spaced(failure)
    exit_code: int | None = 1
    timed_out = False
    duration_ms = 500 + (index * 17)
    if failure == "sandbox_timeout":
        stderr = "sandbox execution timed out"
        exit_code = None
        timed_out = True
        duration_ms = 30000
    elif failure == "docker_daemon_unavailable":
        stderr = "Docker repository sandbox error: daemon unavailable"
        exit_code = None
    elif failure == "memory_limit_exceeded":
        stderr = "process killed after exceeding memory limit"
        exit_code = 137
    elif failure == "cpu_quota_exceeded":
        stderr = "execution exceeded CPU quota"
        exit_code = 124
    elif failure == "pytest_failure":
        stderr = "FAILED tests/test_app.py::test_smoke"
    elif failure == "syntax_error":
        stderr = "SyntaxError: invalid syntax"
    return {
        "exit_code": exit_code,
        "stdout": "",
        "stderr": stderr,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


def sandbox_row(ctx: BatchContext, index: int, failure: str) -> dict[str, Any]:
    return {
        "case_id": f"SANDBOX-{ctx.compact_date}-{index:03d}",
        "batch_id": ctx.batch_id,
        "dataset_version": ctx.dataset_version,
        "agent_target": "sandbox_judge",
        "repo_url": f"https://github.com/CodeReferee-Dataset/sandbox-{failure}-{index:03d}",
        "branch": "main",
        "preflight_report": {
            "cloneable": True,
            "executable": True,
            "detected_stack": "unknown until sandbox clone",
            "reason": "Repository ref is reachable; sandbox will clone and detect executable commands.",
        },
        "execution_result": sandbox_execution_for(failure, index),
        "expected_judge_status": "Fail",
        "expected_failure_type": failure,
        "expected_critic_focus": spaced(failure),
        "source": common_source(),
        "training_usage": training_usage(),
    }


def metrics_for(reason: str, index: int) -> tuple[dict[str, Any], str]:
    metrics = {
        "p95_latency_ms": 180,
        "error_rate": 0.0,
        "cpu_usage_percent": 42,
        "memory_usage_mb": 250,
        "memory_limit_mb": 1024,
        "restart_count": 0,
        "availability": 0.999,
        "request_count": 120 + index,
    }
    status = "Fail"
    if reason in {"all_slo_passed", "boundary_pass", "stable_under_load", "near_limit_pass"}:
        status = "Pass"
    if reason == "latency_slo_violation":
        metrics["p95_latency_ms"] = 900
    elif reason == "error_rate_slo_violation":
        metrics["error_rate"] = 0.08
    elif reason == "cpu_saturation":
        metrics["cpu_usage_percent"] = 94
    elif reason == "memory_pressure":
        metrics["memory_usage_mb"] = 950
    elif reason == "unexpected_restart":
        metrics["restart_count"] = 2
    elif reason == "availability_slo_violation":
        metrics["availability"] = 0.97
    elif reason == "multiple_slo_violations":
        metrics.update({"p95_latency_ms": 1200, "error_rate": 0.2, "cpu_usage_percent": 93, "memory_usage_mb": 990, "restart_count": 3, "availability": 0.93})
    elif reason == "redis_connection_errors":
        metrics.update({"error_rate": 0.02, "redis_connection_errors": 5, "availability": 0.99})
    elif reason == "database_connection_errors":
        metrics.update({"error_rate": 0.03, "db_connection_errors": 4, "availability": 0.985})
    elif reason == "no_traffic_observed":
        metrics.update({"p95_latency_ms": None, "error_rate": None, "request_count": 0})
    elif reason == "missing_metrics":
        metrics.update({"p95_latency_ms": None, "error_rate": None, "cpu_usage_percent": None, "availability": None})
    elif reason == "boundary_pass":
        metrics.update({"p95_latency_ms": 299, "error_rate": 0.01, "cpu_usage_percent": 80, "availability": 0.996})
    elif reason == "latency_boundary_fail":
        metrics["p95_latency_ms"] = 301
    elif reason == "error_boundary_fail":
        metrics["error_rate"] = 0.011
    elif reason == "cpu_boundary_fail":
        metrics["cpu_usage_percent"] = 81
    elif reason == "request_spike_degradation":
        metrics.update({"p95_latency_ms": 650, "error_rate": 0.04, "availability": 0.99})
    elif reason == "cold_start_latency":
        metrics["p95_latency_ms"] = 1100
    elif reason == "stable_under_load":
        metrics.update({"p95_latency_ms": 260, "error_rate": 0.004, "cpu_usage_percent": 72, "memory_usage_mb": 640, "availability": 0.998})
    elif reason == "near_limit_pass":
        metrics.update({"p95_latency_ms": 280, "error_rate": 0.005, "cpu_usage_percent": 78, "memory_usage_mb": 780, "availability": 0.997})
    return metrics, status


def metric_row(ctx: BatchContext, index: int, reason: str) -> dict[str, Any]:
    metrics, status = metrics_for(reason, index)
    return {
        "case_id": f"METRIC-{ctx.compact_date}-{index:03d}",
        "batch_id": ctx.batch_id,
        "dataset_version": ctx.dataset_version,
        "agent_target": "judge",
        "sandbox": {"exit_code": 0, "timed_out": False},
        "metrics": metrics,
        "slo": {
            "p95_latency_ms_max": 300,
            "error_rate_max": 0.01,
            "cpu_usage_percent_max": 80,
            "restart_count_max": 0,
            "availability_min": 0.995,
            "memory_usage_ratio_max": 0.8,
            "db_connection_errors_max": 0,
            "redis_connection_errors_max": 0,
            "request_count_min": 1,
        },
        "expected_judge_status": status,
        "expected_reason_category": reason,
        "expected_evidence_fields": ["exit_code", "timed_out", "p95_latency_ms", "error_rate", "cpu_usage_percent", "restart_count"],
        "source": common_source(),
        "training_usage": training_usage(),
    }


def critic_row(ctx: BatchContext, index: int, failure: str) -> dict[str, Any]:
    log, root_cause, action, risk = CRITIC_LIBRARY[failure]
    return {
        "case_id": f"CRITIC-{ctx.compact_date}-{index:03d}",
        "batch_id": ctx.batch_id,
        "dataset_version": ctx.dataset_version,
        "agent_target": "critic_refiner",
        "failure_type": failure,
        "logs": log,
        "metrics": {"error_rate": round(min(0.01 + index * 0.025, 0.5), 3)},
        "judge_report": {"status": "Fail", "reason_category": failure},
        "expected_critic": {
            "issue": f"Repository failed validation due to {failure}.",
            "root_cause": root_cause,
            "evidence": [log],
            "recommended_action": action,
        },
        "expected_refiner": {
            "summary": root_cause,
            "patch_guidance": [action, "Add regression coverage for this failure mode."],
            "verification_steps": [
                "Re-run validation from the same commit SHA.",
                "Confirm the failing signal is resolved.",
                "Confirm no new SLO violation appears.",
            ],
            "risk": risk,
        },
        "source": common_source(),
        "training_usage": training_usage(),
    }


def local_repo_row(ctx: BatchContext, index: int, stack: str) -> dict[str, Any]:
    should_pass = index % 4 != 0
    failure_type = None if should_pass else "fixture_expected_failure"
    return {
        "case_id": f"LOCAL-REPO-{ctx.compact_date}-{index:03d}",
        "batch_id": ctx.batch_id,
        "dataset_version": ctx.dataset_version,
        "agent_target": "fixture_repo",
        "repo_name": f"sample-{stack}-{'pass' if should_pass else 'fail'}-{index:03d}",
        "stack": stack,
        "purpose": f"{stack} fixture for {'pass' if should_pass else 'fail'} validation behavior.",
        "files": STACK_FILES[stack],
        "expected_sandbox": {"exit_code": 0 if should_pass else 1, "timed_out": False},
        "expected_judge_status": "Pass" if should_pass else "Fail",
        "expected_failure_type": failure_type,
        "implementation_priority": "high" if should_pass else "medium",
        "source": common_source(),
        "training_usage": training_usage(),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


def write_readme(path: Path, ctx: BatchContext, distributions: dict[str, dict[str, int]]) -> None:
    lines = [
        f"# {ctx.batch_id}",
        "",
        "Daily synthetic CodeReferee dataset batch.",
        "",
        "## Counts",
        "",
    ]
    for filename in FILES:
        lines.append(f"- `{filename}`: {ROWS_PER_FILE}")
    lines.extend([
        "",
        f"Total: {ROWS_PER_FILE * len(FILES)} rows/specs.",
        "",
        "## Review Policy",
        "",
        "- `human_review_required=true`",
        "- `real_execution_observed=false`",
        "- Do not move these rows into `datasets/codereferee/reviewed/` until a person reviews labels and usefulness.",
        "",
        "## Distribution",
        "",
    ])
    for label, counts in distributions.items():
        lines.append(f"### {label}")
        lines.append("")
        for key, value in counts.items():
            lines.append(f"- `{key}`: {value}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def generate_batch(dataset_dir: Path, batch_date: str, force: bool = False) -> Path:
    compact = batch_date.replace("-", "")
    ctx = BatchContext(
        batch_date=batch_date,
        batch_id=f"batch_{batch_date}",
        compact_date=compact,
        dataset_version=f"{batch_date}.batch.v1",
    )
    batch_dir = dataset_dir / "generated" / "batches" / ctx.batch_id
    if batch_dir.exists() and not force:
        raise SystemExit(f"Batch already exists: {batch_dir}. Use --force to replace it.")
    batch_dir.mkdir(parents=True, exist_ok=True)

    rows_by_file = {
        "preflight_failures.jsonl": [preflight_row(ctx, index + 1, reason) for index, reason in enumerate(cycle(PREFLIGHT_REASONS, ROWS_PER_FILE))],
        "sandbox_failures.jsonl": [sandbox_row(ctx, index + 1, failure) for index, failure in enumerate(cycle(SANDBOX_FAILURES, ROWS_PER_FILE))],
        "metrics_judge_cases.jsonl": [metric_row(ctx, index + 1, reason) for index, reason in enumerate(cycle(METRIC_REASONS, ROWS_PER_FILE))],
        "critic_refiner_cases.jsonl": [critic_row(ctx, index + 1, failure) for index, failure in enumerate(cycle(CRITIC_FAILURES, ROWS_PER_FILE))],
        "local_sample_repo_specs.jsonl": [local_repo_row(ctx, index + 1, stack) for index, stack in enumerate(cycle(LOCAL_STACKS, ROWS_PER_FILE))],
    }

    distributions: dict[str, dict[str, int]] = {}
    for filename, rows in rows_by_file.items():
        write_jsonl(batch_dir / filename, rows)
        label = FILES[filename]
        if filename == "preflight_failures.jsonl":
            values = [row["expected_reason_category"] for row in rows]
        elif filename == "sandbox_failures.jsonl":
            values = [row["expected_failure_type"] for row in rows]
        elif filename == "metrics_judge_cases.jsonl":
            values = [row["expected_reason_category"] for row in rows]
        elif filename == "critic_refiner_cases.jsonl":
            values = [row["failure_type"] for row in rows]
        else:
            values = [row["stack"] for row in rows]
        distributions[label] = dict(Counter(values))

    distribution = {
        "batch_id": ctx.batch_id,
        "total_rows": ROWS_PER_FILE * len(FILES),
        "total_cases": ROWS_PER_FILE * len(FILES),
        "files": {filename: ROWS_PER_FILE for filename in FILES},
        "distributions": distributions,
        "policy": {
            "human_review_required": True,
            "real_execution_observed": False,
            "intended_use": "generated seed batch only",
        },
    }
    (batch_dir / "distribution.json").write_text(json.dumps(distribution, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_readme(batch_dir / "README.md", ctx, distributions)
    return batch_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a daily CodeReferee synthetic dataset batch.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Batch date in YYYY-MM-DD format")
    parser.add_argument(
        "--dataset-dir",
        default=str(Path(__file__).resolve().parents[1] / "datasets" / "codereferee"),
        help="Path to datasets/codereferee",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing batch for the same date")
    args = parser.parse_args()

    batch_dir = generate_batch(Path(args.dataset_dir), args.date, force=args.force)
    print(f"generated {ROWS_PER_FILE * len(FILES)} rows in {batch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
