#!/usr/bin/env python3
"""Validate CodeReferee dataset seed files.

This runner checks dataset structure before the data is used for evaluation,
LLM expansion, or fine-tuning conversion. It intentionally does not call the
LLM or the live validation workflow.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


CANONICAL_EXPECTED_COUNTS = {
    "generated/preflight_failures.jsonl": 210,
    "generated/sandbox_failures.jsonl": 210,
    "generated/metrics_judge_cases.jsonl": 220,
    "generated/critic_refiner_cases.jsonl": 220,
    "generated/local_sample_repo_specs.jsonl": 205,
}

BATCH_EXPECTED_COUNTS = {
    "preflight_failures.jsonl": 200,
    "sandbox_failures.jsonl": 200,
    "metrics_judge_cases.jsonl": 200,
    "critic_refiner_cases.jsonl": 200,
    "local_sample_repo_specs.jsonl": 200,
}

SOURCE_TYPES = {"synthetic_manual_seed", "synthetic_generated_seed"}
RISK_LEVELS = {"low", "medium", "high"}
JUDGE_STATUS = {"Pass", "Fail"}
Validator = Callable[[str, dict[str, Any], int, list[str]], None]


@dataclass
class DatasetReport:
    file_counts: dict[str, int]
    total_rows: int
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no} row must be a JSON object")
            rows.append(row)
    return rows


def validate_common(
    rel: str,
    row: dict[str, Any],
    index: int,
    seen: set[str],
    errors: list[str],
    batch_id: str | None = None,
) -> None:
    location = f"{rel}:{index}"
    case_id = row.get("case_id")
    if not case_id or not isinstance(case_id, str):
        errors.append(f"{location} missing string case_id")
    elif case_id in seen:
        errors.append(f"{location} duplicate case_id {case_id}")
    else:
        seen.add(case_id)

    if batch_id is not None and row.get("batch_id") != batch_id:
        errors.append(f"{location} batch_id must be {batch_id}")

    source = row.get("source")
    if not isinstance(source, dict):
        errors.append(f"{location} missing source object")
        return

    if source.get("type") not in SOURCE_TYPES:
        errors.append(f"{location} invalid source.type {source.get('type')!r}")
    if source.get("llm_generated_allowed") is not True:
        errors.append(f"{location} llm_generated_allowed must be true")
    if source.get("human_review_required") is not True:
        errors.append(f"{location} human_review_required must be true")
    if source.get("real_execution_observed") is not False:
        errors.append(f"{location} real_execution_observed must be false for generated seed data")


def validate_preflight(rel: str, row: dict[str, Any], index: int, errors: list[str]) -> None:
    location = f"{rel}:{index}"
    required = ["repo_url", "expected_stage", "expected_status", "expected_reason_category", "expected_cloneable"]
    for field in required:
        if field not in row:
            errors.append(f"{location} missing {field}")
    if row.get("expected_stage") != "preflight":
        errors.append(f"{location} expected_stage must be preflight")
    if row.get("expected_status") != "Fail":
        errors.append(f"{location} expected_status must be Fail")
    if row.get("expected_cloneable") is not False:
        errors.append(f"{location} expected_cloneable must be false")


def validate_sandbox(rel: str, row: dict[str, Any], index: int, errors: list[str]) -> None:
    location = f"{rel}:{index}"
    preflight = row.get("preflight_report")
    execution = row.get("execution_result")
    if not isinstance(preflight, dict):
        errors.append(f"{location} missing preflight_report object")
    if not isinstance(execution, dict):
        errors.append(f"{location} missing execution_result object")
        return
    for field in ["exit_code", "stdout", "stderr", "timed_out", "duration_ms"]:
        if field not in execution:
            errors.append(f"{location} execution_result missing {field}")
    if row.get("expected_judge_status") not in JUDGE_STATUS:
        errors.append(f"{location} invalid expected_judge_status")
    if not row.get("expected_failure_type"):
        errors.append(f"{location} missing expected_failure_type")


def validate_metrics(rel: str, row: dict[str, Any], index: int, errors: list[str]) -> None:
    location = f"{rel}:{index}"
    if not isinstance(row.get("sandbox"), dict):
        errors.append(f"{location} missing sandbox object")
    if not isinstance(row.get("metrics"), dict):
        errors.append(f"{location} missing metrics object")
    if not isinstance(row.get("slo"), dict):
        errors.append(f"{location} missing slo object")
    if row.get("expected_judge_status") not in JUDGE_STATUS:
        errors.append(f"{location} invalid expected_judge_status")
    if not row.get("expected_reason_category"):
        errors.append(f"{location} missing expected_reason_category")


def validate_critic_refiner(rel: str, row: dict[str, Any], index: int, errors: list[str]) -> None:
    location = f"{rel}:{index}"
    critic = row.get("expected_critic")
    refiner = row.get("expected_refiner")
    if not isinstance(critic, dict):
        errors.append(f"{location} missing expected_critic object")
    else:
        for field in ["issue", "root_cause", "evidence", "recommended_action"]:
            if field not in critic:
                errors.append(f"{location} expected_critic missing {field}")
        if not isinstance(critic.get("evidence"), list):
            errors.append(f"{location} expected_critic.evidence must be a list")
    if not isinstance(refiner, dict):
        errors.append(f"{location} missing expected_refiner object")
    else:
        for field in ["summary", "patch_guidance", "verification_steps", "risk"]:
            if field not in refiner:
                errors.append(f"{location} expected_refiner missing {field}")
        if not isinstance(refiner.get("patch_guidance"), list):
            errors.append(f"{location} expected_refiner.patch_guidance must be a list")
        if not isinstance(refiner.get("verification_steps"), list):
            errors.append(f"{location} expected_refiner.verification_steps must be a list")
        if refiner.get("risk") not in RISK_LEVELS:
            errors.append(f"{location} invalid refiner risk")


def validate_local_repo_specs(rel: str, row: dict[str, Any], index: int, errors: list[str]) -> None:
    location = f"{rel}:{index}"
    for field in ["repo_name", "stack", "purpose", "files", "expected_sandbox", "expected_judge_status"]:
        if field not in row:
            errors.append(f"{location} missing {field}")
    if row.get("expected_judge_status") not in JUDGE_STATUS:
        errors.append(f"{location} invalid expected_judge_status")
    if not isinstance(row.get("files"), list) or not row.get("files"):
        errors.append(f"{location} files must be a non-empty list")


VALIDATORS_BY_FILENAME: dict[str, Validator] = {
    "preflight_failures.jsonl": validate_preflight,
    "sandbox_failures.jsonl": validate_sandbox,
    "metrics_judge_cases.jsonl": validate_metrics,
    "critic_refiner_cases.jsonl": validate_critic_refiner,
    "local_sample_repo_specs.jsonl": validate_local_repo_specs,
}


def validate_jsonl_file(
    base: Path,
    rel: str,
    expected_count: int,
    validator: Validator,
    errors: list[str],
    batch_id: str | None = None,
) -> int:
    path = base / rel
    if not path.exists():
        errors.append(f"missing file {rel}")
        return 0

    try:
        rows = load_jsonl(path)
    except ValueError as exc:
        errors.append(str(exc))
        return 0

    count = len(rows)
    if count != expected_count:
        errors.append(f"{rel} expected {expected_count}, got {count}")

    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        validate_common(rel, row, index, seen, errors, batch_id=batch_id)
        validator(rel, row, index, errors)
    return count


def validate_distribution(base: Path, batch_dir: Path, errors: list[str]) -> None:
    rel = str(batch_dir.relative_to(base) / "distribution.json")
    path = base / rel
    if not path.exists():
        errors.append(f"missing file {rel}")
        return
    try:
        distribution = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{rel} invalid JSON: {exc}")
        return
    if distribution.get("batch_id") != batch_dir.name:
        errors.append(f"{rel} batch_id must be {batch_dir.name}")
    if distribution.get("total_cases") != sum(BATCH_EXPECTED_COUNTS.values()):
        errors.append(f"{rel} total_cases must be {sum(BATCH_EXPECTED_COUNTS.values())}")


def evaluate_dataset(base: Path) -> DatasetReport:
    errors: list[str] = []
    file_counts: dict[str, int] = {}
    total_rows = 0

    for rel, expected_count in CANONICAL_EXPECTED_COUNTS.items():
        filename = Path(rel).name
        count = validate_jsonl_file(base, rel, expected_count, VALIDATORS_BY_FILENAME[filename], errors)
        file_counts[rel] = count
        total_rows += count

    batches_dir = base / "generated" / "batches"
    if batches_dir.exists():
        for batch_dir in sorted(path for path in batches_dir.iterdir() if path.is_dir()):
            validate_distribution(base, batch_dir, errors)
            for filename, expected_count in BATCH_EXPECTED_COUNTS.items():
                rel = str(batch_dir.relative_to(base) / filename)
                count = validate_jsonl_file(
                    base,
                    rel,
                    expected_count,
                    VALIDATORS_BY_FILENAME[filename],
                    errors,
                    batch_id=batch_dir.name,
                )
                file_counts[rel] = count
                total_rows += count

    expected_total = sum(CANONICAL_EXPECTED_COUNTS.values())
    if batches_dir.exists():
        batch_count = len([path for path in batches_dir.iterdir() if path.is_dir()])
        expected_total += batch_count * sum(BATCH_EXPECTED_COUNTS.values())
    if total_rows != expected_total:
        errors.append(f"total expected {expected_total}, got {total_rows}")

    return DatasetReport(file_counts=file_counts, total_rows=total_rows, errors=errors)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CodeReferee dataset seed files.")
    parser.add_argument(
        "--dataset-dir",
        default=str(Path(__file__).resolve().parents[2] / "datasets" / "codereferee"),
        help="Path to datasets/codereferee",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON report")
    args = parser.parse_args()

    report = evaluate_dataset(Path(args.dataset_dir))
    if args.json:
        print(
            json.dumps(
                {
                    "passed": report.passed,
                    "file_counts": report.file_counts,
                    "total_rows": report.total_rows,
                    "errors": report.errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for rel, count in report.file_counts.items():
            print(f"{rel}: {count}")
        print(f"total: {report.total_rows}")
        if report.passed:
            print("dataset quality check passed")
        else:
            print("dataset quality check failed")
            for error in report.errors[:50]:
                print(f"- {error}")
            if len(report.errors) > 50:
                print(f"... and {len(report.errors) - 50} more errors")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
