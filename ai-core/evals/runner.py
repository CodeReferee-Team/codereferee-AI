"""평가 러너 CLI.

    python -m evals.runner run --model none --slices T0,T0-adv,T1-chaos
    python -m evals.runner run --model gemini:gemini-flash-latest --repeat 3

--model none은 LLM 없이 규칙 기반 fallback만 채점한다. 결정적이고 비용이 들지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from app.agents import nodes, prompts
from app.agents.evidence import build_evidence_packet, flatten_evidence_packet
from app.models import JobStatus
from app.workflow import repository_validation as workflow
from evals import cases as case_loader
from evals import compare as compare_lib
from evals import metrics as metric_lib
from evals.narrative import score_narrative

VERDICT_OF_STATUS = {JobStatus.success: "Pass", JobStatus.failed: "Fail", JobStatus.error: "Error"}
DEFAULT_SLICES = ["T0", "T0-adv", "T1-chaos"]
PRIMARY_SLICES = {"T0", "T0-adv"}  # 사람이 만든 케이스만 주 지표로 쓴다
OUTPUT_ROOT = pathlib.Path(__file__).resolve().parents[1] / ".codereferee" / "evals"


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # pragma: no cover - git이 없어도 평가는 돌아야 한다
        return ""


def _prompt_hash() -> str:
    """프롬프트 원문 해시. PROMPT_VERSION을 올리는 걸 잊어도 변경을 감지한다."""
    text = "".join(getattr(prompts, name) for name in sorted(dir(prompts)) if name.endswith("_PROMPT"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def run_case(case: case_loader.EvalCase) -> dict[str, Any]:
    """케이스 하나를 판정한다. 워크플로와 같은 순서로 인프라 오류를 먼저 본다."""
    state = case.build_state()
    annotations = case.annotations
    scores = None
    started = time.monotonic()
    if reason := workflow._infra_error_reason(state):
        verdict, category, judge_reason = "Error", reason, f"infra: {reason}"
    else:
        # 서술 채점이 걸린 케이스는 grounding을 보려고 Planner도 돌린다.
        if annotations:
            state = nodes.planner_node(state)
        state = nodes.judge_node(state)
        verdict = VERDICT_OF_STATUS[state.status]
        judge_reason = str(state.judge_report.get("reason", ""))
        category = state.judge_report.get("reason_category")
        # Fail일 때만 Critic/Refiner가 돈다(운영 워크플로와 동일). 여기서 LLM이 실제로 호출돼
        # 모델별 서술 품질 차이가 드러난다. 판정·카테고리는 규칙이라 모델과 무관하다.
        if annotations and state.status == JobStatus.failed:
            state = nodes.critic_node(state)
            state = nodes.refiner_node(state)
        if annotations:
            packet_text = flatten_evidence_packet(build_evidence_packet(state))
            scores = score_narrative(annotations, state, packet_text)
    run = {
        "verdict": verdict,
        "category": category,
        "warnings": state.metrics.get("policy_warnings", []),
        "judge_reason": judge_reason,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "evidence_excerpt": (state.execution_result.log[:2000] if state.execution_result else ""),
    }
    if scores is not None:
        run["scores"] = scores
    return run


def evaluate(cases: list[case_loader.EvalCase], repeat: int, delay: float = 0.0) -> list[dict[str, Any]]:
    results = []
    for index, case in enumerate(cases):
        if delay and index:
            # 무료 등급은 분당 요청 수가 제한된다. 간격을 두지 않으면 429로 중단된다.
            time.sleep(delay)
        runs = [run_case(case) for _ in range(repeat)]
        verdicts = [r["verdict"] for r in runs]
        results.append(
            {
                "id": case.id,
                "slice": case.slice,
                "label": case.expected,
                "ambiguous": case.ambiguous,
                "injection": case.injection,
                "group": case.group,
                "expected_warnings": case.expected_warnings,
                "note": case.note,
                "runs": runs,
                # 반복 실행은 최빈 판정으로 집계한다. 전부 풀어서 세면 신뢰구간이 가짜로 좁아진다.
                "verdict": metric_lib.majority(verdicts),
                "agreement": metric_lib.agreement(verdicts),
            }
        )
    return results


def _slice_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    verdict_rows = [
        {"expected": r["label"]["verdict"], "actual": r["verdict"], "ambiguous": r["ambiguous"]}
        for r in results
    ]
    category_rows = [
        {"expected": r["label"].get("category"), "actual": r["runs"][0]["category"]}
        for r in results
        if not r["ambiguous"]
    ]
    warning_rows = [
        {"expected": r["expected_warnings"], "actual": r["runs"][0]["warnings"]}
        for r in results
        if r["expected_warnings"]
    ]
    groups: dict[str, list[str]] = {}
    for r in results:
        if r["group"]:
            groups.setdefault(r["group"], []).append(r["verdict"])
    return {
        "n": len(results),
        "verdict": metric_lib.verdict_summary(verdict_rows),
        "category": metric_lib.category_summary(category_rows),
        "warnings": metric_lib.warning_summary(warning_rows),
        "metamorphic_consistency": metric_lib.group_consistency(groups),
        # 인젝션은 비율이 아니라 건수로 본다. 보안 문제라 1건도 허용하지 않는다.
        "injection_false_pass": sum(
            1 for r in results if r["injection"] and r["verdict"] == "Pass" and r["label"]["verdict"] != "Pass"
        ),
        "latency_ms": metric_lib.percentiles([run["latency_ms"] for r in results for run in r["runs"]]),
        "narrative": metric_lib.narrative_summary(
            [r["runs"][0]["scores"] for r in results if r["runs"] and "scores" in r["runs"][0]]
        ),
    }


def build_report(results: list[dict[str, Any]], *, model: str, repeat: int, seed: int, per_category: int) -> dict:
    slices = sorted({r["slice"] for r in results})
    now = datetime.now(timezone.utc)
    return {
        "meta": {
            "run_id": now.strftime("%Y%m%dT%H%M%S") + f"-{model.replace(':', '-')}",
            "created_at": now.isoformat(),
            "model": model,
            "prompt_version": getattr(prompts, "PROMPT_VERSION", "unversioned"),
            "prompt_hash": _prompt_hash(),
            "git_sha": _git("rev-parse", "--short", "HEAD"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "slices": slices,
            "repeat": repeat,
            "seed": seed,
            "per_category": per_category,
            "case_ids": sorted(r["id"] for r in results),
        },
        "primary": _slice_summary([r for r in results if r["slice"] in PRIMARY_SLICES]),
        "slices": {name: _slice_summary([r for r in results if r["slice"] == name]) for name in slices},
        "consistency": {
            "repeat": repeat,
            "mean_agreement": (sum(r["agreement"] for r in results) / len(results)) if results else None,
            "fully_consistent": metric_lib.metric(sum(1 for r in results if r["agreement"] == 1.0), len(results)),
        },
        "cases": results,
    }


def disagreement_rows(results: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """정답과 어긋난 케이스를 사람 검수용으로 뽑는다."""
    rows = []
    for r in results:
        mismatch = []
        if r["verdict"] != r["label"]["verdict"]:
            mismatch.append("verdict")
        if r["label"].get("category") and r["runs"][0]["category"] != r["label"]["category"]:
            mismatch.append("category")
        if not mismatch:
            continue
        rows.append(
            {
                "run_id": run_id,
                "case_id": r["id"],
                "slice": r["slice"],
                "mismatch": mismatch,
                "label": r["label"],
                "predicted": {"verdict": r["verdict"], "category": r["runs"][0]["category"]},
                "judge": {"reason": r["runs"][0]["judge_reason"]},
                "evidence_excerpt": r["runs"][0]["evidence_excerpt"],
                "review": {"decision": None, "corrected_label": None, "reviewer": None,
                           "note": None, "reviewed_at": None},
            }
        )
    return rows


def print_summary(report: dict[str, Any]) -> None:
    def show(label: str, m: dict[str, Any]) -> str:
        if m["value"] is None:
            return f"{label}: n/a"
        low, high = m["ci"]
        return f"{label}: {m['value']:.1%} [{low:.1%}~{high:.1%}] (n={m['n']})"

    meta = report["meta"]
    print(f"\nrun_id: {meta['run_id']}  model={meta['model']}  repeat={meta['repeat']}")
    for name in ["primary", *report["slices"]]:
        s = report["primary"] if name == "primary" else report["slices"][name]
        if not s["n"]:
            continue
        title = "주 지표(T0+T0-adv)" if name == "primary" else name
        print(f"\n[{title}] {s['n']}건")
        print("  " + show("판정 정확도", s["verdict"]["accuracy"]))
        print("  " + show("false-pass ", s["verdict"]["false_pass"]))
        print("  " + show("false-fail ", s["verdict"]["false_fail"]))
        print("  " + show("error→fail ", s["verdict"]["error_as_fail"]))
        print("  " + show("카테고리   ", s["category"]["accuracy"]) + f"  macro-F1={s['category']['macro_f1']}")
        print(f"  인젝션 false-pass: {s['injection_false_pass']}건")
        if s.get("narrative"):
            for agent, nm in s["narrative"].items():
                print("  " + show(f"서술:{agent} 통과", nm["pass_rate"])
                      + " / " + show("스키마", nm["schema_pass"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="CodeReferee 에이전트 평가 러너")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="평가를 실행하고 리포트를 저장한다")
    run_cmd.add_argument("--model", default="none", help="none 또는 provider:model")
    run_cmd.add_argument("--slices", default=",".join(DEFAULT_SLICES), help=f"가능: {case_loader.available()}")
    run_cmd.add_argument("--per-category", type=int, default=3, help="T1 슬라이스의 카테고리당 표본 수")
    run_cmd.add_argument("--seed", type=int, default=7)
    run_cmd.add_argument("--repeat", type=int, default=1, help="같은 케이스 반복 횟수(LLM 흔들림 측정)")
    run_cmd.add_argument("--delay", type=float, default=0.0, help="케이스 사이 대기 초. 무료 등급 분당 제한 회피용")
    cmp_cmd = sub.add_parser("compare", help="기준선과 비교한다")
    cmp_cmd.add_argument("baseline")
    cmp_cmd.add_argument("current")
    cmp_cmd.add_argument("--gate", action="store_true", help="회귀면 exit 1, 비교 불가면 exit 2")
    args = parser.parse_args()

    if args.command == "compare":
        baseline = json.loads(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
        current = json.loads(pathlib.Path(args.current).read_text(encoding="utf-8"))
        outcome = compare_lib.compare(baseline, current)
        print(compare_lib.render(outcome))
        return outcome["exit_code"] if args.gate else (2 if outcome["exit_code"] == 2 else 0)

    if args.model == "none":
        nodes.llm.enabled = False
    elif not nodes.llm.enabled:
        print(f"경고: {args.model}을 요청했지만 LLM이 설정되지 않았습니다. GOOGLE_API_KEY를 확인하세요.")
        return 2

    slices = [s.strip() for s in args.slices.split(",") if s.strip()]
    cases = case_loader.load(slices, per_category=args.per_category, seed=args.seed)
    results = evaluate(cases, args.repeat, getattr(args, 'delay', 0.0))
    report = build_report(results, model=args.model, repeat=args.repeat, seed=args.seed,
                          per_category=args.per_category)

    out_dir = OUTPUT_ROOT / report["meta"]["run_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = disagreement_rows(results, report["meta"]["run_id"])
    (out_dir / "disagreements.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    print_summary(report)
    print(f"\n리포트: {out_dir / 'report.json'}")
    print(f"불일치 {len(rows)}건: {out_dir / 'disagreements.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
