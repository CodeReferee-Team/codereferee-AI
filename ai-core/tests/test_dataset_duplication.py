"""Tests for the dataset duplication measurement script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from measure_dataset_duplication import (  # noqa: E402
    canonical_key,
    main,
    measure,
    template_key,
)


def write_batch(directory: Path, name: str, rows: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def row(case_id: str, batch_id: str, reason: str, index: int) -> dict:
    return {
        "case_id": case_id,
        "batch_id": batch_id,
        "dataset_version": f"{batch_id}.v1",
        "input": {"repo_url": f"https://example.com/{reason}-{index:03d}"},
        "expected": {"reason_category": reason},
    }


def test_canonical_key_ignores_identity_fields():
    a = row("PREFLIGHT-20260101-001", "batch_2026-01-01", "branch_not_found", 1)
    b = row("PREFLIGHT-20260102-001", "batch_2026-01-02", "branch_not_found", 1)
    assert canonical_key(a) == canonical_key(b)


def test_canonical_key_separates_different_content():
    a = row("A", "batch_1", "branch_not_found", 1)
    b = row("B", "batch_1", "commit_not_found", 1)
    assert canonical_key(a) != canonical_key(b)


def test_template_key_collapses_digit_differences():
    a = row("A", "batch_1", "branch_not_found", 1)
    b = row("B", "batch_1", "branch_not_found", 2)
    assert canonical_key(a) != canonical_key(b)
    assert template_key(a) == template_key(b)


def test_identical_batches_report_full_duplication(tmp_path: Path):
    rows_a = [row(f"A-{i}", "batch_2026-01-01", "branch_not_found", i) for i in range(10)]
    rows_b = [row(f"B-{i}", "batch_2026-01-02", "branch_not_found", i) for i in range(10)]
    write_batch(tmp_path / "batch_2026-01-01", "preflight_failures", rows_a)
    write_batch(tmp_path / "batch_2026-01-02", "preflight_failures", rows_b)

    report = measure([tmp_path])

    assert report.total_rows == 20
    assert report.unique_rows == 10
    assert report.duplication_percent == pytest.approx(50.0)


def test_distinct_rows_report_no_duplication(tmp_path: Path):
    rows = [row(f"A-{i}", "batch_1", f"reason_{i}", i) for i in range(10)]
    write_batch(tmp_path / "batch_1", "preflight_failures", rows)

    report = measure([tmp_path])

    assert report.total_rows == 10
    assert report.unique_rows == 10
    assert report.duplication_percent == pytest.approx(0.0)


def test_template_duplication_catches_index_only_variation(tmp_path: Path):
    rows = [row(f"A-{i}", "batch_1", "branch_not_found", i) for i in range(10)]
    write_batch(tmp_path / "batch_1", "preflight_failures", rows)

    report = measure([tmp_path])

    assert report.duplication_percent == pytest.approx(0.0)
    assert report.unique_templates == 1
    assert report.template_duplication_percent == pytest.approx(90.0)


def test_per_file_breakdown(tmp_path: Path):
    write_batch(tmp_path, "preflight_failures", [row("A", "b1", "x", 1), row("B", "b1", "x", 1)])
    write_batch(tmp_path, "sandbox_failures", [row("C", "b1", "y", 1)])

    report = measure([tmp_path])

    assert report.per_file["preflight_failures.jsonl"].duplication_percent == pytest.approx(50.0)
    assert report.per_file["sandbox_failures.jsonl"].duplication_percent == pytest.approx(0.0)


def test_empty_input_reports_zero(tmp_path: Path):
    report = measure([tmp_path])

    assert report.total_rows == 0
    assert report.duplication_percent == pytest.approx(0.0)


def test_same_filename_in_two_directories_accumulates(tmp_path: Path):
    """A batch dir per date means the same filename appears many times.

    The per-file row count and unique count must describe the same set of
    rows, otherwise the breakdown contradicts the overall number.
    """
    first = [row(f"A-{i}", "batch_1", f"reason_{i}", i) for i in range(10)]
    second = [row(f"B-{i}", "batch_2", f"other_{i}", i) for i in range(10)]
    write_batch(tmp_path / "batch_1", "preflight_failures", first)
    write_batch(tmp_path / "batch_2", "preflight_failures", second)

    report = measure([tmp_path])
    file_report = report.per_file["preflight_failures.jsonl"]

    assert file_report.total_rows == 20
    assert file_report.unique_rows == 20
    assert file_report.duplication_percent == pytest.approx(0.0)
    assert file_report.duplication_percent == pytest.approx(report.duplication_percent)


def test_missing_path_is_an_error(tmp_path: Path):
    """A gate pointed at a path that does not exist must not pass silently."""
    with pytest.raises(SystemExit):
        measure([tmp_path / "does-not-exist"])


def test_main_fails_when_threshold_set_but_nothing_measured(tmp_path: Path, capsys):
    """Zero rows means the gate checked nothing. That is a failure, not a pass."""
    exit_code = main([str(tmp_path), "--max-duplication", "5"])

    assert exit_code == 1
    assert "no rows" in capsys.readouterr().out.lower()


def test_main_without_threshold_tolerates_zero_rows(tmp_path: Path):
    exit_code = main([str(tmp_path)])

    assert exit_code == 0
