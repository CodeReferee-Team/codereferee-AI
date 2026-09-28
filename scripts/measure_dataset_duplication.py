#!/usr/bin/env python3
"""Measure how much of a generated dataset is duplicated.

The daily batch generator was paused on 2026-09-26 because 68 batches were
96.8% duplicated. This script turns that after-the-fact observation into a
number the generator can be held to.

Two metrics, because they answer different questions:

- duplication: rows that repeat once identity fields (case_id, batch_id,
  dataset_version) are stripped. This is the cross-batch metric -- "we
  generated the same rows again".
- template duplication: rows that repeat once digits are also normalised.
  This catches rows that differ only by a counter (repo-001, repo-002), which
  read as distinct but carry no new information.

Exit code is 1 when a threshold is exceeded, so it can gate a loop or CI.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

IDENTITY_FIELDS = ("case_id", "batch_id", "dataset_version")
DIGITS = re.compile(r"\d+")


def _strip_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in IDENTITY_FIELDS}


def canonical_key(row: dict[str, Any]) -> str:
    """Row content with identity fields removed."""
    return json.dumps(_strip_identity(row), ensure_ascii=False, sort_keys=True)


def template_key(row: dict[str, Any]) -> str:
    """Row content with identity fields removed and every number normalised."""
    return DIGITS.sub("#", canonical_key(row))


@dataclass
class FileReport:
    """Stats for one filename, accumulated across every directory it appears in.

    A batch directory per date means the same filename shows up many times, so
    the sets must accumulate -- counting rows across batches while measuring
    uniqueness within one of them would report a number nothing supports.
    """

    total_rows: int = 0
    rows: set[str] = field(default_factory=set)
    templates: set[str] = field(default_factory=set)

    @property
    def unique_rows(self) -> int:
        return len(self.rows)

    @property
    def unique_templates(self) -> int:
        return len(self.templates)

    @property
    def duplication_percent(self) -> float:
        return _percent(self.total_rows, self.unique_rows)

    @property
    def template_duplication_percent(self) -> float:
        return _percent(self.total_rows, self.unique_templates)


@dataclass
class Report:
    total_rows: int = 0
    unique_rows: int = 0
    unique_templates: int = 0
    per_file: dict[str, FileReport] = field(default_factory=dict)

    @property
    def duplication_percent(self) -> float:
        return _percent(self.total_rows, self.unique_rows)

    @property
    def template_duplication_percent(self) -> float:
        return _percent(self.total_rows, self.unique_templates)


def _percent(total: int, unique: int) -> float:
    if total == 0:
        return 0.0
    return (total - unique) / total * 100


def _iter_jsonl(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if not path.exists():
            raise SystemExit(f"no such path: {path}")
        if path.is_dir():
            yield from sorted(path.rglob("*.jsonl"))
        elif path.suffix == ".jsonl":
            yield path
        else:
            raise SystemExit(f"not a directory or .jsonl file: {path}")


def _read_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{line_number}: invalid JSON: {error}") from error


def measure(paths: Iterable[Path]) -> Report:
    report = Report()
    seen_rows: set[str] = set()
    seen_templates: set[str] = set()

    for path in _iter_jsonl(paths):
        file_report = report.per_file.setdefault(path.name, FileReport())

        for row in _read_rows(path):
            canonical = canonical_key(row)
            template = template_key(row)

            report.total_rows += 1
            file_report.total_rows += 1
            seen_rows.add(canonical)
            seen_templates.add(template)
            file_report.rows.add(canonical)
            file_report.templates.add(template)

    report.unique_rows = len(seen_rows)
    report.unique_templates = len(seen_templates)
    return report


def format_report(report: Report) -> str:
    lines = [
        f"rows        {report.total_rows}",
        f"unique      {report.unique_rows}  (duplication {report.duplication_percent:.1f}%)",
        f"templates   {report.unique_templates}  (template duplication {report.template_duplication_percent:.1f}%)",
    ]
    if report.per_file:
        lines.append("")
        lines.append(f"{'file':34s} {'rows':>6s} {'unique':>7s} {'dup%':>7s} {'tmpl%':>7s}")
        for name, file_report in sorted(report.per_file.items()):
            lines.append(
                f"{name:34s} {file_report.total_rows:6d} {file_report.unique_rows:7d} "
                f"{file_report.duplication_percent:6.1f}% {file_report.template_duplication_percent:6.1f}%"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path, help="Batch directories or .jsonl files to measure")
    parser.add_argument("--max-duplication", type=float, default=None, help="Fail when duplication exceeds this percent")
    parser.add_argument(
        "--max-template-duplication",
        type=float,
        default=None,
        help="Fail when template duplication exceeds this percent",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)
    gated = args.max_duplication is not None or args.max_template_duplication is not None

    report = measure(args.paths)

    if args.json:
        print(
            json.dumps(
                {
                    "total_rows": report.total_rows,
                    "unique_rows": report.unique_rows,
                    "duplication_percent": round(report.duplication_percent, 2),
                    "unique_templates": report.unique_templates,
                    "template_duplication_percent": round(report.template_duplication_percent, 2),
                    "per_file": {
                        name: {
                            "total_rows": file_report.total_rows,
                            "unique_rows": file_report.unique_rows,
                            "duplication_percent": round(file_report.duplication_percent, 2),
                            "template_duplication_percent": round(file_report.template_duplication_percent, 2),
                        }
                        for name, file_report in sorted(report.per_file.items())
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_report(report))

    failed = False
    if gated and report.total_rows == 0:
        # A gate that measured nothing has not held any line. Say so loudly:
        # an empty or mistyped path must not read as a clean batch.
        print("FAIL: no rows measured, so nothing was checked")
        failed = True
    if args.max_duplication is not None and report.duplication_percent > args.max_duplication:
        print(f"FAIL: duplication {report.duplication_percent:.1f}% exceeds {args.max_duplication:.1f}%")
        failed = True
    if args.max_template_duplication is not None and report.template_duplication_percent > args.max_template_duplication:
        print(
            f"FAIL: template duplication {report.template_duplication_percent:.1f}% "
            f"exceeds {args.max_template_duplication:.1f}%"
        )
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
