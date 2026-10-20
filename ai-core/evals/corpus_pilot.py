"""학습 코퍼스 생성 파일럿.

목적은 데이터를 모으는 것이 아니라 두 숫자를 재는 것이다.
1. 수율: Refiner 패치가 재실행을 통과하는 비율
2. 처리량: 케이스당 실제 소요 시간

이 두 값을 모르면 풀버전 생성기를 만들 근거가 없다.

결함은 diff로 주입한다. sandbox가 clone 직후 `git apply`하는 경로(B2)를 그대로 쓰므로
레포를 fork하거나 push할 필요가 없다. 수정 패치는 결함 diff 뒤에 이어 붙여 재실행한다(B3와 동일).

    python -m evals.corpus_pilot --out .codereferee/corpus/pilot.jsonl

SANDBOX_BASE_URL을 비워서 로컬 Docker 경로로 돌려야 한다. 외부 sandbox는 패치를 받지 못한다.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from app.agents import nodes
from app.agents.patching import inspect_diff
from app.models import AgentState, JobStatus
from app.repository.preflight import repository_preflight_runner
from app.sandbox.docker_runner import sandbox_runner
from app.workflow import repository_validation as workflow

# baseline이 통과하는 레포만 쓴다. baseline이 실패하면 결함이 원인인지 구분할 수 없다.
REPOSITORIES = (
    "https://github.com/benjaminp/six",
    "https://github.com/pytest-dev/iniconfig",
)


@dataclass
class Fault:
    name: str
    detectable: bool  # 현재 sandbox가 잡을 수 있는 결함인지
    apply: Callable[[pathlib.Path], None]


def _first_source_file(repo: pathlib.Path) -> pathlib.Path:
    candidates = sorted(p for p in repo.rglob("*.py") if ".git" not in p.parts and p.stat().st_size > 200)
    if not candidates:
        raise RuntimeError(f"no python source file in {repo}")
    return candidates[0]


def _break_syntax(repo: pathlib.Path) -> None:
    target = _first_source_file(repo)
    target.write_text(target.read_text(encoding="utf-8") + "\ndef broken(:\n    pass\n", encoding="utf-8")


def _break_indentation(repo: pathlib.Path) -> None:
    target = _first_source_file(repo)
    target.write_text(
        target.read_text(encoding="utf-8") + "\ndef misindented():\nreturn 1\n", encoding="utf-8"
    )


def _add_missing_dependency(repo: pathlib.Path) -> None:
    (repo / "requirements.txt").write_text("definitely-not-a-real-package-zzz\n", encoding="utf-8")


def _pin_impossible_version(repo: pathlib.Path) -> None:
    (repo / "requirements.txt").write_text("six==99999.0.0\n", encoding="utf-8")


def _silent_logic_change(repo: pathlib.Path) -> None:
    """컴파일은 되지만 동작이 바뀌는 결함. 현재 sandbox는 잡지 못한다.

    실제 레포에서 우리 검증의 사각지대를 재기 위해 일부러 넣는다. 수율 계산에서는 제외한다.
    """
    target = _first_source_file(repo)
    target.write_text(
        target.read_text(encoding="utf-8") + "\ndef always_true():\n    return False\n", encoding="utf-8"
    )


FAULTS = (
    Fault("syntax_error", True, _break_syntax),
    Fault("indentation_error", True, _break_indentation),
    Fault("missing_dependency", True, _add_missing_dependency),
    Fault("impossible_version_pin", True, _pin_impossible_version),
    Fault("silent_logic_change", False, _silent_logic_change),
)


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def build_fault_diff(repository_url: str, fault: Fault) -> str:
    """레포를 얕게 clone해 결함을 적용하고 diff를 뽑는다. 작업 트리는 버린다."""
    workdir = tempfile.mkdtemp(prefix="codereferee-fault-")
    try:
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", repository_url, workdir],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"clone failed: {clone.stderr.strip()}")
        repo = pathlib.Path(workdir)
        fault.apply(repo)
        _git(repo, "add", "-A")
        # --cached로 신규 파일(requirements.txt)도 diff에 포함시킨다.
        diff = _git(repo, "diff", "--cached", "--no-color").stdout
        if not diff.strip():
            raise RuntimeError("fault produced an empty diff")
        return diff
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _state_for(repository_url: str, execution_result, fault_diff: str) -> AgentState:
    state = AgentState(
        job_id="pilot", repository_url=repository_url, status=JobStatus.running
    )
    state.preflight_report = repository_preflight_runner.run(repository_url)
    state.execution_result = execution_result
    state.metrics = workflow._metrics_from_execution(state)
    state.sre_metrics = workflow._sre_metrics_from_execution(state)
    # 수정 패치는 결함이 적용된 파일 위에 올라간다. 무엇이 적용됐는지 알려주지 않으면
    # 원본 기준으로 패치를 써서 충돌한다(docs/judge-policy.md 6.5와 같은 이유).
    state.metrics["applied_patch"] = fault_diff
    return state


def run_case(repository_url: str, fault: Fault, *, skip_critic: bool = False) -> dict:
    started = time.monotonic()
    record: dict[str, object] = {
        "repository_url": repository_url,
        "fault": fault.name,
        "fault_detectable": fault.detectable,
    }

    fault_diff = build_fault_diff(repository_url, fault)
    record["fault_diff_bytes"] = len(fault_diff.encode("utf-8"))

    faulted = sandbox_runner.run_repository(repository_url, patch_diff=fault_diff)
    record["faulted_exit_code"] = faulted.exit_code
    record["faulted_duration_ms"] = faulted.duration_ms
    if faulted.infra_error:
        record["outcome"] = "infra_error"
        record["infra_error"] = faulted.infra_error
        record["elapsed_seconds"] = round(time.monotonic() - started, 1)
        return record

    state = _state_for(repository_url, faulted, fault_diff)
    state = nodes.judge_node(state)
    record["verdict"] = state.judge_report.get("status")
    record["reason_category"] = state.judge_report.get("reason_category")
    record["evidence"] = state.judge_report.get("evidence")

    if record["verdict"] != "Fail":
        # 결함을 심었는데 통과로 봤다면 우리 검증의 사각지대다.
        record["outcome"] = "not_detected"
        record["elapsed_seconds"] = round(time.monotonic() - started, 1)
        return record

    workflow.attach_source_files(state, applied_patch=fault_diff)
    record["source_files"] = sorted(state.source_files)
    # Critic ablation: 자연어 원인 분석이 수정에 기여하는지 재려면 없이도 돌려봐야 한다.
    record["critic_skipped"] = skip_critic
    if not skip_critic:
        state = nodes.critic_node(state)
    state = nodes.refiner_node(state)
    record["critic_root_cause"] = state.critic_feedback.get("root_cause")
    record["refiner_summary"] = state.refiner_report.get("summary")
    fix_diff = state.refiner_report.get("patch_diff")
    record["patch_check"] = state.metrics.get("patch_check")
    # 재요청이 실제로 발동했는지 보려면 이벤트가 필요하다.
    record["events"] = [e for e in state.events if e.startswith("Refiner:")]
    if not fix_diff:
        record["outcome"] = "no_patch"
        record["elapsed_seconds"] = round(time.monotonic() - started, 1)
        return record

    record["fix_diff"] = fix_diff
    cumulative = fault_diff + ("" if fault_diff.endswith("\n") else "\n") + str(fix_diff)
    gate = inspect_diff(cumulative)
    if not gate.accepted:
        record["outcome"] = "patch_rejected"
        record["reject_reason"] = gate.reason_code
        record["elapsed_seconds"] = round(time.monotonic() - started, 1)
        return record

    rerun = sandbox_runner.run_repository(repository_url, patch_diff=cumulative)
    record["rerun_exit_code"] = rerun.exit_code
    record["rerun_duration_ms"] = rerun.duration_ms
    record["outcome"] = "fixed" if rerun.exit_code == 0 and not rerun.timed_out else "not_fixed"
    record["elapsed_seconds"] = round(time.monotonic() - started, 1)
    return record


def summarize(records: list[dict]) -> dict:
    detectable = [r for r in records if r["fault_detectable"]]
    detected = [r for r in detectable if r.get("verdict") == "Fail"]
    patched = [r for r in detected if r.get("fix_diff")]
    fixed = [r for r in patched if r.get("outcome") == "fixed"]
    blind = [r for r in records if not r["fault_detectable"]]
    elapsed = sorted(float(r["elapsed_seconds"]) for r in records if "elapsed_seconds" in r)
    return {
        "cases": len(records),
        "detection_rate": _ratio(len(detected), len(detectable)),
        "patch_generation_rate": _ratio(len(patched), len(detected)),
        # 수율: 잡아낸 결함 중 패치가 실제로 고친 비율. 이 값이 코퍼스 생성 속도를 결정한다.
        "yield": _ratio(len(fixed), len(detected)),
        "blind_spot_cases": len(blind),
        "blind_spot_missed": sum(1 for r in blind if r.get("outcome") == "not_detected"),
        "seconds_per_case_median": elapsed[len(elapsed) // 2] if elapsed else None,
        "seconds_total": round(sum(elapsed), 1) if elapsed else None,
        "outcomes": {o: sum(1 for r in records if r.get("outcome") == o) for o in
                     sorted({str(r.get("outcome")) for r in records})},
    }


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".codereferee/corpus/pilot.jsonl")
    parser.add_argument("--append", action="store_true", help="기존 출력에 이어 붙인다")
    parser.add_argument("--repos", help="쉼표로 구분한 레포 이름 일부. 없으면 전부")
    parser.add_argument("--faults", help="쉼표로 구분한 결함 이름. 없으면 전부")
    parser.add_argument("--skip-critic", action="store_true", help="Critic을 건너뛴다(ablation)")
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    repositories = REPOSITORIES
    if args.repos:
        wanted = [name.strip() for name in args.repos.split(",") if name.strip()]
        repositories = tuple(url for url in REPOSITORIES if any(name in url for name in wanted))
    faults = FAULTS
    if args.faults:
        wanted = {name.strip() for name in args.faults.split(",") if name.strip()}
        faults = tuple(fault for fault in FAULTS if fault.name in wanted)

    records: list[dict] = []
    if args.append and out.exists():
        records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    with out.open("a" if args.append else "w", encoding="utf-8") as handle:
        for repository_url in repositories:
            for fault in faults:
                try:
                    record = run_case(repository_url, fault, skip_critic=args.skip_critic)
                except Exception as exc:  # 한 케이스의 실패로 배치 전체를 잃지 않는다
                    record = {
                        "repository_url": repository_url,
                        "fault": fault.name,
                        "fault_detectable": fault.detectable,
                        "outcome": "case_error",
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                records.append(record)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                print(
                    f"{repository_url.split('/')[-1]:12} {fault.name:24} "
                    f"{record.get('outcome'):14} {record.get('reason_category') or '-':28} "
                    f"{record.get('elapsed_seconds')}s",
                    flush=True,
                )

    summary = summarize(records)
    summary["critic_skipped"] = bool(args.skip_critic)
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    out.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
