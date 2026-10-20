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
    "preflight_failures.jsonl": "preflight.expected.reason_category",
    "sandbox_failures.jsonl": "sandbox.expected.failure_type",
    "metrics_judge_cases.jsonl": "sre.expected.reason_category",
    "critic_refiner_cases.jsonl": "critic.input.failure_type",
    "local_sample_repo_specs.jsonl": "local_repo.input.stack",
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


def _base_row(ctx: BatchContext, prefix: str, index: int, agent_target: str) -> dict[str, Any]:
    return {
        "case_id": f"{prefix}-{ctx.compact_date}-{index:03d}",
        "batch_id": ctx.batch_id,
        "dataset_version": ctx.dataset_version,
        "agent_target": agent_target,
        "source": common_source(),
        "training_usage": training_usage(),
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
    row = _base_row(ctx, "PREFLIGHT", index, "preflight")
    row.update(
        {
            "input": {
                "repo_url": f"https://github.com/CodeReferee-Dataset/{reason}-{index:03d}",
                "branch": branch,
                "commit_sha": commit_sha,
                "input_type": reason,
            },
            "expected": {
                "stage": "preflight",
                "status": "Fail",
                "reason_category": reason,
                "cloneable": False,
                "evidence_contains": [spaced(reason)],
            },
        }
    )
    return row

# 실패 유형별로 그 도구가 실제로 찍는 문구와 종료 코드.
#
# 이전에는 stderr 기본값이 `spaced(failure)`였다. 라벨 dockerfile_missing인 케이스의
# stderr가 "dockerfile missing"이어서 정답이 로그에 그대로 적혀 있었다. 로그를 읽는
# 판정자는 공짜로 맞히고, 종료 코드만 보는 규칙은 맞힐 수 없다. 그리고 로그를 읽는 쪽이
# LLM 판정이 레포에 심어둔 지시에 속은 경로다(docs/judge-policy.md 8절).
#
# 종료 코드는 sandbox 스크립트의 계약을 따른다. 86 manifest 없음, 87 러너 없음,
# 89 검증할 것 없음, 126 실행 권한 없음, 137 OOM.
SANDBOX_LOGS: dict[str, dict[str, Any]] = {
    "dependency_install_failed": {
        "stderr": "ERROR: Could not find a version that satisfies the requirement flask==99.0.0\n"
        "ERROR: No matching distribution found for flask==99.0.0",
    },
    "pytest_failure": {
        "stderr": "FAILED tests/test_app.py::test_smoke - assert 1 == 2\n1 failed, 3 passed in 0.41s",
    },
    "sandbox_timeout": {
        "stderr": "Command timed out after 600 seconds",
        "exit_code": None,
        "timed_out": True,
        "duration_ms": 600000,
    },
    "memory_limit_exceeded": {"stderr": "Killed", "exit_code": 137},
    "cpu_quota_exceeded": {
        "stderr": "cgroup cpu.stat: nr_throttled increased while the build was running",
    },
    "missing_env": {"stderr": 'KeyError: "SERVICE_TOKEN"'},
    "entrypoint_import_error": {
        "stderr": 'ImportError: cannot import name "app" from "main" (/tmp/repository/main.py)',
    },
    "port_bind_failure": {"stderr": "OSError: [Errno 98] Address already in use"},
    "gradle_permission_denied": {"stderr": "/bin/sh: ./gradlew: Permission denied", "exit_code": 126},
    "gradle_test_failed": {
        "stderr": "> Task :test FAILED\nFAILURE: Build failed with an exception.\n"
        "Execution failed for task ':test'.",
    },
    "maven_permission_denied": {"stderr": "/bin/sh: ./mvnw: Permission denied", "exit_code": 126},
    "maven_test_failed": {
        "stderr": "[ERROR] Tests run: 4, Failures: 1, Errors: 0, Skipped: 0\n[ERROR] BUILD FAILURE",
    },
    "npm_install_failed": {
        "stderr": "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree",
    },
    "npm_test_missing": {"stderr": "package.json has no test script to verify", "exit_code": 89},
    "docker_build_failed": {
        "stderr": 'ERROR: failed to solve: process "/bin/sh -c pip install -r requirements.txt" '
        "did not complete successfully: exit code: 1",
    },
    "dockerfile_missing": {
        "stderr": "No supported project manifest found",
        "exit_code": 86,
    },
    "redis_unavailable": {
        "stderr": "redis.exceptions.ConnectionError: Error 111 connecting to redis:6379. "
        "Connection refused.",
    },
    "postgres_unavailable": {
        "stderr": "psycopg2.OperationalError: could not connect to server: Connection refused\n"
        '\tIs the server running on host "postgres" and accepting TCP/IP connections on port 5432?',
    },
    # 아래 세 문구는 sandbox 스크립트가 실제로 찍는 것과 같다(app/sandbox/docker_runner.py).
    "unsupported_stack": {
        "stderr": "Node toolchain is not available in the sandbox image",
        "exit_code": 87,
    },
    "no_smoke_command": {
        "stderr": "Repository has no Gradle wrapper (./gradlew)",
        "exit_code": 87,
    },
    "permission_denied_runtime": {"stderr": "/bin/sh: ./run.sh: Permission denied", "exit_code": 126},
    "syntax_error": {
        "stderr": 'File "/tmp/repository/app/main.py", line 42\n    def broken(:\n'
        "               ^\nSyntaxError: invalid syntax",
    },
    "package_lock_mismatch": {
        "stderr": "npm ERR! code EUSAGE\nnpm ERR! npm ci can only install packages when your "
        "package.json and package-lock.json are in sync",
    },
    "missing_settings_gradle": {
        "stderr": "FAILURE: Build failed with an exception.\n* What went wrong:\n"
        "Settings file 'settings.gradle' not found",
    },
    "docker_daemon_unavailable": {
        "stderr": "Docker repository sandbox error: Error while fetching server API version",
        "exit_code": None,
        # 판정은 로그를 읽지 않는다. 이 신호가 있어야 Error로 끝낼 수 있다.
        "infra_error": "docker_daemon_unreachable",
    },
}


def sandbox_execution_for(failure: str, index: int) -> dict[str, Any]:
    if failure not in SANDBOX_LOGS:
        # 라벨을 로그에 적어 메우지 않는다. 새 유형을 넣을 때 로그도 함께 쓰게 만든다.
        raise KeyError(f"SANDBOX_LOGS에 {failure}의 로그와 종료 코드를 먼저 적어야 한다")
    spec = SANDBOX_LOGS[failure]
    execution = {
        "exit_code": spec.get("exit_code", 1),
        "stdout": "",
        "stderr": spec["stderr"],
        "timed_out": spec.get("timed_out", False),
        "duration_ms": spec.get("duration_ms", 500 + (index * 17)),
    }
    if infra_error := spec.get("infra_error"):
        execution["infra_error"] = infra_error
    return execution


def sandbox_row(ctx: BatchContext, index: int, failure: str) -> dict[str, Any]:
    row = _base_row(ctx, "SANDBOX", index, "sandbox_judge")
    row.update(
        {
            "input": {
                "repo_url": f"https://github.com/CodeReferee-Dataset/sandbox-{failure}-{index:03d}",
                "branch": "main",
                "preflight_report": {
                    "cloneable": True,
                    "executable": True,
                    "detected_stack": "unknown until sandbox clone",
                    "reason": "Repository ref is reachable; sandbox will clone and detect executable commands.",
                },
                "execution_result": sandbox_execution_for(failure, index),
            },
            "expected": {
                "judge_status": "Fail",
                "failure_type": failure,
                "critic_focus": spaced(failure),
            },
        }
    )
    return row

def metrics_for(reason: str, index: int) -> tuple[dict[str, Any], str]:
    sli = {
        "availability_percent": 99.9,
        "p95_latency_ms": 180,
        "p99_latency_ms": 260,
        "error_rate": 0.0,
        "throughput_rps": 120 + index,
    }
    status = "Fail"
    if reason in {"all_slo_passed", "boundary_pass", "stable_under_load", "near_limit_pass"}:
        status = "Pass"
    if reason == "latency_slo_violation":
        sli.update({"p95_latency_ms": 900, "p99_latency_ms": 1400})
    elif reason == "error_rate_slo_violation":
        sli["error_rate"] = 0.08
    elif reason == "cpu_saturation":
        sli.update({"error_rate": 0.02, "throughput_rps": 30})
    elif reason == "memory_pressure":
        sli.update({"error_rate": 0.02, "p95_latency_ms": 520})
    elif reason == "unexpected_restart":
        sli.update({"availability_percent": 98.5, "error_rate": 0.03})
    elif reason == "availability_slo_violation":
        sli["availability_percent"] = 97.0
    elif reason == "multiple_slo_violations":
        sli.update({"availability_percent": 93.0, "p95_latency_ms": 1200, "p99_latency_ms": 2500, "error_rate": 0.2, "throughput_rps": 20})
    elif reason in {"redis_connection_errors", "database_connection_errors"}:
        sli.update({"availability_percent": 99.0, "error_rate": 0.03})
    elif reason == "no_traffic_observed":
        sli.update({"p95_latency_ms": None, "p99_latency_ms": None, "error_rate": None, "throughput_rps": 0})
    elif reason == "missing_metrics":
        sli.update({"availability_percent": None, "p95_latency_ms": None, "p99_latency_ms": None, "error_rate": None, "throughput_rps": None})
    elif reason == "boundary_pass":
        sli.update({"availability_percent": 99.9, "p95_latency_ms": 300, "p99_latency_ms": 1000, "error_rate": 0.01, "throughput_rps": 50})
    elif reason == "latency_boundary_fail":
        sli["p95_latency_ms"] = 301
    elif reason == "error_boundary_fail":
        sli["error_rate"] = 0.011
    elif reason == "cpu_boundary_fail":
        sli.update({"p95_latency_ms": 320, "throughput_rps": 45})
    elif reason == "request_spike_degradation":
        sli.update({"availability_percent": 99.0, "p95_latency_ms": 650, "p99_latency_ms": 1300, "error_rate": 0.04})
    elif reason == "cold_start_latency":
        sli.update({"p95_latency_ms": 1100, "p99_latency_ms": 1800})
    elif reason == "stable_under_load":
        sli.update({"availability_percent": 99.95, "p95_latency_ms": 260, "p99_latency_ms": 700, "error_rate": 0.004, "throughput_rps": 160})
    elif reason == "near_limit_pass":
        sli.update({"availability_percent": 99.91, "p95_latency_ms": 280, "p99_latency_ms": 900, "error_rate": 0.005, "throughput_rps": 70})
    return sli, status

def metric_row(ctx: BatchContext, index: int, reason: str) -> dict[str, Any]:
    sli, status = metrics_for(reason, index)
    allowed_error_rate = 0.01
    observed_error_rate = sli.get("error_rate")
    budget_remaining = None
    if isinstance(observed_error_rate, (int, float)):
        budget_remaining = round(max(0.0, (allowed_error_rate - observed_error_rate) / allowed_error_rate * 100), 2)
    row = _base_row(ctx, "METRIC", index, "judge")
    row.update(
        {
            "input": {
                "sandbox": {"exit_code": 0, "timed_out": False, "duration_ms": 4200 + index},
                "chaos": {
                    "scenario": "network_latency" if index % 2 else "resource_pressure",
                    "target": "app_container",
                    "duration_sec": 60,
                    "recovered": status == "Pass",
                    "recovery_time_sec": 12 if status == "Pass" else None,
                },
                "sli": sli,
                "slo": {
                    "availability_percent_min": 99.9,
                    "p95_latency_ms_max": 300,
                    "p99_latency_ms_max": 1000,
                    "error_rate_max": allowed_error_rate,
                    "throughput_rps_min": 50,
                },
                "error_budget": {
                    "allowed_error_rate": allowed_error_rate,
                    "observed_error_rate": observed_error_rate,
                    "budget_remaining_percent": budget_remaining,
                },
            },
            "expected": {
                "judge_status": status,
                "reason_category": reason,
                "evidence_keys": [
                    "availability_percent",
                    "p95_latency_ms",
                    "p99_latency_ms",
                    "error_rate",
                    "budget_remaining_percent",
                ],
            },
        }
    )
    return row

def critic_row(ctx: BatchContext, index: int, failure: str) -> dict[str, Any]:
    log, root_cause, action, risk = CRITIC_LIBRARY[failure]
    error_rate = round(min(0.01 + index * 0.025, 0.5), 3)
    row = _base_row(ctx, "CRITIC", index, "critic_refiner")
    row.update(
        {
            "input": {
                "failure_type": failure,
                "logs": log,
                "sli": {"error_rate": error_rate},
                "judge_report": {"status": "Fail", "reason_category": failure},
            },
            "expected": {
                "critic": {
                    "issue": f"Repository failed validation due to {failure}.",
                    "root_cause": root_cause,
                    "evidence": [log],
                    "recommended_action": action,
                },
                "refiner": {
                    "summary": root_cause,
                    "patch_guidance": [action, "Add regression coverage for this failure mode."],
                    "verification_steps": [
                        "Re-run validation from the same commit SHA.",
                        "Confirm the failing signal is resolved.",
                        "Confirm no new SLO violation appears.",
                    ],
                    "risk": risk,
                },
            },
        }
    )
    return row

def local_repo_row(ctx: BatchContext, index: int, stack: str) -> dict[str, Any]:
    should_pass = index % 4 != 0
    failure_type = None if should_pass else "fixture_expected_failure"
    row = _base_row(ctx, "LOCAL-REPO", index, "fixture_repo")
    row.update(
        {
            "input": {
                "repo_name": f"sample-{stack}-{'pass' if should_pass else 'fail'}-{index:03d}",
                "stack": stack,
                "purpose": f"{stack} fixture for {'pass' if should_pass else 'fail'} validation behavior.",
                "files": STACK_FILES[stack],
            },
            "expected": {
                "sandbox": {"exit_code": 0 if should_pass else 1, "timed_out": False},
                "judge_status": "Pass" if should_pass else "Fail",
                "failure_type": failure_type,
                "implementation_priority": "high" if should_pass else "medium",
            },
        }
    )
    return row

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
            values = [row["expected"]["reason_category"] for row in rows]
        elif filename == "sandbox_failures.jsonl":
            values = [row["expected"]["failure_type"] for row in rows]
        elif filename == "metrics_judge_cases.jsonl":
            values = [row["expected"]["reason_category"] for row in rows]
        elif filename == "critic_refiner_cases.jsonl":
            values = [row["input"]["failure_type"] for row in rows]
        else:
            values = [row["input"]["stack"] for row in rows]
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
