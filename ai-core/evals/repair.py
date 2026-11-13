"""Refiner 패치가 결함을 실제로 고치는지 잰다.

    python -m evals.repair run --model none
    python -m evals.repair run --model oracle
    python -m evals.repair run --model gemini:gemini-flash-latest

판정 평가(evals.runner)는 "Fail이라고 맞게 말했는가"만 본다. 이 평가는 그다음을 본다. 결함이 있는
작은 레포를 실제로 돌려 실패시키고, Critic과 Refiner가 낸 편집을 우리 가드와 재검증 루프에
그대로 태워, 패치를 넣은 재실행이 통과하는지 센다. 루프는 운영 코드(_run_refinement_rounds)다.

샌드박스는 1층 스크립트를 컨테이너 없이 sh로 돌린다. 케이스 레포는 우리가 만든 것이고 의존성이
없어 네트워크를 쓰지 않는다. 컨테이너 격리와 리소스 한도는 재지 않는다.

모델:
- none: LLM 없이 규칙 fallback. 편집을 만들지 못하므로 수리율 0이 정상이다. 하한이다.
- oracle: 사람이 적은 정답 편집을 fallback에 끼운다. 하네스와 문맥 수집이 맞는지 보는 상한이다.
  정답 편집인데 수리가 안 되면 모델이 아니라 우리 파이프라인(파일 첨부, 가드, 재실행)의 결함이다.
- provider:model: 실제 Refiner. GOOGLE_API_KEY 등 LLM 설정이 있어야 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.agents import nodes
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.sandbox import docker_runner
from app.workflow import repository_validation as workflow
from evals import metrics as eval_metrics

CASES_DIR = Path(__file__).parent / "repair_cases"
SCRIPT_TIMEOUT_SECONDS = 300


def load_cases(ids: list[str] | None = None) -> list[dict[str, Any]]:
    cases = []
    for case_file in sorted(CASES_DIR.glob("*/case.json")):
        case = json.loads(case_file.read_text(encoding="utf-8"))
        case["repo_dir"] = case_file.parent / "repo"
        if ids and case["id"] not in ids:
            continue
        cases.append(case)
    return cases


def _init_repository(source: Path, target: Path) -> None:
    shutil.copytree(source, target)
    git = ["git", "-C", str(target), "-c", "user.email=eval@codereferee", "-c", "user.name=eval"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "case"], check=True)


class ShellSandbox:
    """1층 검증 스크립트를 컨테이너 없이 돌린다. 결과 형태는 로컬 Docker 경로와 같다."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.runs = 0

    def run_repository(self, repository_url: str, branch=None, commit_sha=None, patch_diff=None, **_: Any) -> SandboxResult:
        self.runs += 1
        run_dir = self.workdir / f"run-{self.runs}"
        run_dir.mkdir()
        patch_path = ""
        if patch_diff:
            patch_path = str(run_dir / docker_runner.PATCH_FILENAME)
            Path(patch_path).write_text(patch_diff, encoding="utf-8")
        clone_dir = str(run_dir / "clone")
        script = docker_runner._repository_validation_script(repository_url, branch, commit_sha, patch_path)
        # clone 위치가 고정이라 그대로 두면 실행끼리 부딪힌다. 로그는 원래 경로로 되돌려
        # source_context가 컨테이너 경로를 지우는 동작까지 운영과 같게 한다.
        script = script.replace("/tmp/repository", clone_dir)
        try:
            completed = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                                       timeout=SCRIPT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return SandboxResult(exit_code=None, timed_out=True)
        logs = (completed.stdout + completed.stderr).replace(clone_dir, "/tmp/repository")
        report, clean_logs = docker_runner._extract_sandbox_report(logs)
        return SandboxResult(
            exit_code=completed.returncode,
            stdout=clean_logs if completed.returncode == 0 else "",
            stderr="" if completed.returncode == 0 else clean_logs,
            sandbox_report=report,
        )


@contextmanager
def _patched(target: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def _oracle_refiner(case: dict[str, Any]):
    original = nodes._fallback_refiner

    def refiner(state: AgentState) -> dict[str, object]:
        report = original(state)
        report["edits"] = [dict(edit) for edit in case["oracle_edits"]]
        return report

    return refiner


def run_case(case: dict[str, Any], model: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codereferee-repair-") as tmp:
        root = Path(tmp)
        source = root / "source"
        _init_repository(case["repo_dir"], source)
        url = f"file://{source}"
        sandbox = ShellSandbox(root)

        state = AgentState(job_id=case["id"], repository_url=url, status=JobStatus.running)
        state.preflight_report = RepositoryPreflightReport(repository_url=url, cloneable=True, executable=True)

        refiner_hook = _oracle_refiner(case) if model == "oracle" else nodes._fallback_refiner
        with _patched(workflow.sandbox_runner, "run_repository", sandbox.run_repository), \
                _patched(nodes, "_fallback_refiner", refiner_hook):
            state.execution_result = sandbox.run_repository(url)
            state.metrics = workflow._metrics_from_execution(state)
            state.sre_metrics = workflow._sre_metrics_from_execution(state)
            state = nodes.judge_node(state)
            initial = dict(state.judge_report)
            workflow.attach_source_files(state)
            attached = sorted(state.source_files)
            state = nodes.critic_node(state)
            state = nodes.refiner_node(state)
            patch_check = dict(state.metrics.get("patch_check") or {})
            state = workflow._run_refinement_rounds(state, lambda *a, **k: None)

        rounds = list(state.refine_rounds)
        targets = sorted({edit["path"] for edit in case["oracle_edits"]})
        return {
            "id": case["id"],
            "defect": case["defect"],
            "expected_category": case["expected_category"],
            "initial_verdict": initial.get("status"),
            "initial_category": initial.get("reason_category"),
            "target_attached": all(path in attached for path in targets),
            "attached": attached,
            "patch_produced": bool(rounds),
            "patch_rejection": patch_check.get("reason_code") if not rounds else None,
            "repaired": any(r.get("after_judge_status") == "Pass" for r in rounds),
            "rounds": len(rounds),
            # 제출된 레포의 판정은 패치가 통과해도 Fail로 남아야 한다(judge-policy 6.5).
            "final_verdict": state.judge_report.get("status"),
        }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = [r for r in rows if r["initial_verdict"] == "Fail"]
    n = len(attempted)
    return {
        "n": len(rows),
        "initial_detected": eval_metrics.metric(n, len(rows)),
        "initial_category": eval_metrics.metric(
            sum(r["initial_category"] == r["expected_category"] for r in rows), len(rows)),
        "target_attached": eval_metrics.metric(sum(r["target_attached"] for r in attempted), n),
        "patch_produced": eval_metrics.metric(sum(r["patch_produced"] for r in attempted), n),
        "repaired": eval_metrics.metric(sum(r["repaired"] for r in attempted), n),
        "submitted_verdict_kept": eval_metrics.metric(sum(r["final_verdict"] == "Fail" for r in attempted), n),
        "rejections": sorted({r["patch_rejection"] for r in attempted if r["patch_rejection"]}),
    }


def _fmt(value: dict[str, Any]) -> str:
    if value.get("value") is None:
        return "-"
    return f"{value['value'] * 100:.1f}% (n={value['n']})"


def render(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"model={report['meta']['model']}  cases={s['n']}",
        f"초기 실패 탐지      {_fmt(s['initial_detected'])}",
        f"초기 카테고리 일치  {_fmt(s['initial_category'])}",
        f"고칠 파일 첨부      {_fmt(s['target_attached'])}",
        f"패치 생성           {_fmt(s['patch_produced'])}",
        f"수리 성공           {_fmt(s['repaired'])}",
        f"제출 판정 유지      {_fmt(s['submitted_verdict_kept'])}",
    ]
    if s["rejections"]:
        lines.append(f"패치 거부 사유      {', '.join(s['rejections'])}")
    lines.append("")
    for row in report["cases"]:
        mark = "수리" if row["repaired"] else ("패치 없음" if not row["patch_produced"] else "실패")
        lines.append(f"- {row['id']}: {row['initial_category']} → {mark} (rounds={row['rounds']})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.repair")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--model", default="none", help="none, oracle, 또는 provider:model")
    run_cmd.add_argument("--cases", default="", help="쉼표로 구분한 케이스 id")
    run_cmd.add_argument("--out", default="evals/reports")
    args = parser.parse_args(argv)

    if args.model in ("none", "oracle"):
        nodes.llm.enabled = False
    elif not nodes.llm.enabled:
        print(f"{args.model}을 요청했지만 LLM이 설정되지 않았습니다. GOOGLE_API_KEY를 확인하세요.")
        return 2

    cases = load_cases([c for c in args.cases.split(",") if c] or None)
    rows = [run_case(case, args.model) for case in cases]
    now = datetime.now(timezone.utc)
    report = {
        "meta": {"model": args.model, "run_id": now.strftime("%Y%m%dT%H%M%S") + f"-repair-{args.model.replace(':', '-')}",
                 "case_ids": [c["id"] for c in cases]},
        "summary": summarize(rows),
        "cases": rows,
    }
    os.makedirs(args.out, exist_ok=True)
    path = Path(args.out) / f"{report['meta']['run_id']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report))
    print(f"\n리포트: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
