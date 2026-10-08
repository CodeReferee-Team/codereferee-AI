"""AI 검증 결과를 사람이 읽는 PDF 추천 리포트로 렌더한다.

Critic(원인 분석)과 Refiner(수정 권장)의 출력을 받아 "무엇이 문제이고, 어디를 어떻게
고치면 되는지"를 한 장짜리 리포트로 만든다. git apply용 정확한 diff가 아니라 사람이
읽고 판단하는 권장안이 목적이므로, 소형 무료 모델(qwen2.5-coder)의 산출로 충분하다.

입력은 AgentState가 아니라 평범한 dict다(테스트·재사용을 위해 파이프라인과 분리).
report_from_state로 AgentState에서 그 dict를 뽑는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fpdf import FPDF

_FONT_DIR = Path(__file__).parent / "fonts"
_FONT_REGULAR = _FONT_DIR / "Pretendard-Regular.ttf"
_FONT_BOLD = _FONT_DIR / "Pretendard-Bold.ttf"

_INK = (30, 30, 30)
_MUTED = (110, 110, 110)
_RULE = (210, 210, 210)
_CODE_BG = (245, 245, 245)
_FAIL = (176, 0, 32)
_PASS = (15, 110, 86)

_CATEGORY_KO = {
    "chaos_recovery_exceeds_expected_bound": "카오스 복구가 기대 상한을 초과",
    "chaos_error_budget_exhausted": "카오스 복구가 월간 에러버짓을 소진",
    "chaos_not_recovered": "카오스 후 서비스가 복구되지 않음",
    "availability_slo_violation": "가용성 SLO 미달",
    "error_rate_slo_violation": "오류율 SLO 초과",
    "chaos_recovered_within_budget": "카오스에서 예산 내 복구",
    "all_checks_passed": "모든 검사 통과",
}


class _Report(FPDF):
    def header(self) -> None:  # noqa: D401 - fpdf hook
        return

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Pretendard", size=8)
        self.set_text_color(*_MUTED)
        self.cell(0, 8, f"CodeReferee · {self.page_no()}", align="C")


def report_from_state(state: Any) -> dict[str, Any]:
    """AgentState에서 리포트 dict를 뽑는다. 없는 섹션은 빈 dict로 둔다."""
    return {
        "repository_url": getattr(state, "repository_url", ""),
        "status": getattr(getattr(state, "status", None), "value", str(getattr(state, "status", ""))),
        "judge": dict(getattr(state, "judge_report", {}) or {}),
        "critic": dict(getattr(state, "critic_feedback", {}) or {}),
        "refiner": dict(getattr(state, "refiner_report", {}) or {}),
    }


def build_pdf_report(report: dict[str, Any], out_path: str | Path) -> Path:
    """report dict를 PDF로 렌더하고 저장 경로를 돌려준다."""
    if not _FONT_REGULAR.is_file() or not _FONT_BOLD.is_file():
        raise FileNotFoundError(
            f"Pretendard 폰트가 없다: {_FONT_DIR}. 한글 렌더에 필요하다."
        )

    judge = report.get("judge") or {}
    critic = report.get("critic") or {}
    refiner = report.get("refiner") or {}
    status = (judge.get("status") or report.get("status") or "").strip()
    category = judge.get("reason_category") or ""

    pdf = _Report(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_font("Pretendard", "", str(_FONT_REGULAR))
    pdf.add_font("Pretendard", "B", str(_FONT_BOLD))
    pdf.add_page()

    _title(pdf, "CodeReferee 검증 리포트")
    _meta_line(pdf, "레포지토리", report.get("repository_url") or "-")
    _meta_line(pdf, "생성 시각", report.get("generated_at") or _now())
    _verdict(pdf, status, category)
    _rule(pdf)

    if refiner.get("summary"):
        _section(pdf, "요약")
        _body(pdf, str(refiner["summary"]))

    if critic.get("issue") or critic.get("root_cause"):
        _section(pdf, "발견된 문제")
        if critic.get("issue"):
            _body(pdf, str(critic["issue"]))
        if critic.get("root_cause"):
            _label_body(pdf, "원인", str(critic["root_cause"]))

    edits = refiner.get("edits") or []
    guidance = refiner.get("patch_guidance") or []
    if edits or guidance or critic.get("recommended_action"):
        _section(pdf, "권장 수정 (어디를 어떻게)")
        if critic.get("recommended_action"):
            _label_body(pdf, "조치", str(critic["recommended_action"]))
        for edit in edits:
            _edit_block(pdf, edit)
        for item in guidance:
            _bullet(pdf, str(item))

    steps = refiner.get("verification_steps") or []
    if steps:
        _section(pdf, "검증 방법")
        for i, step in enumerate(steps, 1):
            _bullet(pdf, str(step), marker=f"{i}.")

    evidence = (critic.get("evidence") or judge.get("evidence") or [])
    if evidence:
        _section(pdf, "증거")
        for item in evidence:
            _bullet(pdf, str(item))

    if refiner.get("risk"):
        _section(pdf, "위험도")
        _body(pdf, {"low": "낮음", "medium": "중간", "high": "높음"}.get(refiner["risk"], str(refiner["risk"])))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _title(pdf: FPDF, text: str) -> None:
    pdf.set_font("Pretendard", "B", 20)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 10, text)
    pdf.ln(1)


def _meta_line(pdf: FPDF, label: str, value: str) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Pretendard", "B", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(26, 6, label, new_x="RIGHT", new_y="TOP")
    pdf.set_font("Pretendard", "", 9)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 6, value, new_x="LMARGIN", new_y="NEXT")


def _verdict(pdf: FPDF, status: str, category: str) -> None:
    pdf.ln(1)
    is_fail = status.lower() == "fail"
    pdf.set_font("Pretendard", "B", 11)
    pdf.set_text_color(*(_FAIL if is_fail else _PASS))
    label = "판정 불가" if not status else ("실패" if is_fail else "통과")
    ko = _CATEGORY_KO.get(category, category)
    pdf.multi_cell(0, 7, f"판정: {label}" + (f"  —  {ko}" if ko else ""))
    pdf.set_text_color(*_INK)


def _section(pdf: FPDF, text: str) -> None:
    pdf.ln(3)
    pdf.set_font("Pretendard", "B", 13)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 8, text)
    pdf.ln(0.5)


def _body(pdf: FPDF, text: str) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Pretendard", "", 10.5)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")


def _label_body(pdf: FPDF, label: str, text: str) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Pretendard", "B", 10.5)
    pdf.set_text_color(*_MUTED)
    pdf.multi_cell(0, 6, label, new_x="LMARGIN", new_y="NEXT")
    _body(pdf, text)


def _bullet(pdf: FPDF, text: str, marker: str = "•") -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Pretendard", "", 10.5)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 6, f"{marker}  {text}", new_x="LMARGIN", new_y="NEXT")


def _edit_block(pdf: FPDF, edit: dict[str, Any]) -> None:
    path = str(edit.get("path", "")).strip()
    find = str(edit.get("find", ""))
    replace = str(edit.get("replace", ""))
    if not path:
        return
    pdf.ln(1)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Pretendard", "B", 10)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 6, f"파일: {path}", new_x="LMARGIN", new_y="NEXT")
    _code_line(pdf, f"- {find}", (176, 0, 32))
    _code_line(pdf, f"+ {replace}", (15, 110, 86))


def _code_line(pdf: FPDF, text: str, color: tuple[int, int, int]) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Pretendard", "", 9.5)
    pdf.set_fill_color(*_CODE_BG)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, 6, text, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_INK)


def _rule(pdf: FPDF) -> None:
    pdf.ln(2)
    pdf.set_draw_color(*_RULE)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(2)


def _demo() -> None:
    """알려진 단일 replica 케이스로 샘플 리포트를 만들어 본다. 깨지면 바로 드러난다."""
    report = {
        "repository_url": "https://github.com/CodeReferee-Team/codereferee-chaos-demo",
        "judge": {
            "status": "Fail",
            "reason_category": "chaos_recovery_exceeds_expected_bound",
            "reason": "recovery 37.8s exceeds the bound 22.0s implied by the workload configuration.",
            "evidence": ["chaos.replicas=1", "recovery 37.8s exceeds bound 22.0s"],
        },
        "critic": {
            "issue": "서비스가 pod 장애를 무중단으로 견디지 못한다.",
            "root_cause": "단일 replica라 그 pod가 죽으면 교체본이 준비될 때까지 전체 서비스가 중단되고, 그 복구 시간이 기대 상한을 넘는다.",
            "evidence": ["chaos.replicas=1", "policy_warnings=chaos_single_replica_topology"],
            "recommended_action": "살아있는 pod가 복구 중에도 트래픽을 받도록 replica를 2개 이상으로 둔다.",
        },
        "refiner": {
            "summary": "단일 replica 배포는 pod 장애 시 가용성을 모두 잃는다. 중복성을 위해 replica를 늘린다.",
            "patch_guidance": ["복구 중 살아있는 pod가 서빙하도록 replicas를 올린다."],
            "verification_steps": [
                "container-kill 실험을 재실행해 가용성이 유지되고 복구가 상한 내인지 확인한다.",
            ],
            "risk": "low",
            "edits": [{"path": ".codereferee/validation.yaml", "find": "replicas: 1", "replace": "replicas: 2"}],
        },
    }
    import tempfile

    out = build_pdf_report(report, Path(tempfile.gettempdir()) / "codereferee_sample_report.pdf")
    data = out.read_bytes()
    assert data[:5] == b"%PDF-", "PDF 헤더가 아니다"
    assert len(data) > 1024, "PDF가 비정상적으로 작다"
    print(f"OK: {out} ({len(data)} bytes)")


if __name__ == "__main__":
    _demo()
