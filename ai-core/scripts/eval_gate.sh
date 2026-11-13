#!/usr/bin/env bash
# 규칙만으로(LLM 없이) 평가셋을 돌려 커밋된 기준선과 비교한다. CI와 로컬에서 같은 명령을 쓴다.
#
#   bash scripts/eval_gate.sh
#
# 기준선보다 나빠지면 exit 1, 평가셋이 달라 비교할 수 없으면 exit 2.
# 평가셋을 바꿨다면 기준선을 다시 찍고 이전 값을 docs/evaluation-design.md에 남긴다.
set -euo pipefail
cd "$(dirname "$0")/.."

gate() {
  local baseline="$1" slices="$2" per_category="$3" out report
  out="$(python -m evals.runner run --model none --slices "$slices" --per-category "$per_category" 2>/dev/null)"
  report="$(printf '%s\n' "$out" | sed -n 's/^리포트: //p' | tail -1)"
  [ -n "$report" ] || { printf '%s\n' "$out"; echo "리포트 경로를 찾지 못했다" >&2; return 2; }
  echo "== $baseline ($slices)"
  python -m evals.runner compare --gate "$baseline" "$report"
}

# 기준선을 찍은 설정 그대로 돌린다. 슬라이스나 표본 수가 다르면 case_ids가 달라 비교가 안 된다.
gate evals/baselines/fallback.json T0,T0-adv,T1-chaos,T1-metrics,T1-sandbox 2
gate evals/baselines/fallback-chaos-real.json T1-chaos-real 3
