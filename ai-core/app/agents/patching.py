"""Refiner가 만든 패치를 sandbox로 보내기 전에 검사한다.

두 단계다.
1. inspect_diff: 내용만 보고 거른다. 크기 상한, 보호 경로, 레포 밖 경로.
2. check_applies: 실제 레포에 `git apply --check`를 돌려 적용 가능한지 본다.

앞단에서 거르는 이유는 두 가지다. sandbox 실행이 비싸서 적용도 안 될 패치에 수십 초를
쓸 이유가 없고, LLM이 만든 패치를 우리가 실행하므로 CI 설정이나 레포 밖 경로를 건드리는
변경은 애초에 들여보내면 안 된다.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 누적 diff 상한. 이만큼 고쳐야 한다면 자동 수정이 아니라 사람이 볼 문제다.
MAX_DIFF_BYTES = 1_000_000
# 우리가 실행할 패치가 건드리면 안 되는 경로.
PROTECTED_PREFIXES = (".github/", ".git/", ".gitlab-ci", "Jenkinsfile", ".circleci/")

_PATH_LINE = re.compile(r"^(?:---|\+\+\+) (?:[ab]/)?(.+?)(?:\t.*)?$", re.MULTILINE)


@dataclass
class PatchVerdict:
    accepted: bool
    reason_code: str = ""
    reason: str = ""
    touched_paths: list[str] = field(default_factory=list)


def touched_paths(diff: str) -> list[str]:
    paths: list[str] = []
    for raw in _PATH_LINE.findall(diff):
        path = raw.strip()
        if path in ("/dev/null", "") or path in paths:
            continue
        paths.append(path)
    return paths


def inspect_diff(diff: str) -> PatchVerdict:
    """내용만 보고 판단한다. 레포가 없어도 돌아간다."""
    if not diff or not diff.strip():
        return PatchVerdict(False, "patch_empty", "패치가 비어 있다.")

    size = len(diff.encode("utf-8"))
    if size > MAX_DIFF_BYTES:
        return PatchVerdict(
            False, "patch_too_large", f"패치가 {size}바이트로 상한 {MAX_DIFF_BYTES}바이트를 넘는다."
        )

    paths = touched_paths(diff)
    if not paths:
        return PatchVerdict(False, "patch_empty", "패치에서 대상 파일을 찾지 못했다.")

    for path in paths:
        if path.startswith("/") or ".." in Path(path).parts:
            return PatchVerdict(False, "patch_escapes_repository", f"레포 밖 경로를 건드린다: {path}", paths)
        if path.startswith(PROTECTED_PREFIXES):
            return PatchVerdict(False, "patch_touches_protected_path", f"보호 경로를 건드린다: {path}", paths)

    return PatchVerdict(True, touched_paths=paths)


def check_applies(diff: str, repo_path: Path) -> PatchVerdict:
    """`git apply --check`로 실제 적용 가능성을 확인한다. 작업 트리는 바뀌지 않는다."""
    verdict = inspect_diff(diff)
    if not verdict.accepted:
        return verdict

    try:
        result = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=repo_path,
            input=diff,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        return PatchVerdict(False, "patch_check_unavailable", "git 실행 파일을 찾지 못했다.", verdict.touched_paths)
    except subprocess.TimeoutExpired:
        return PatchVerdict(
            False, "patch_check_timeout", "git apply --check가 시간 안에 끝나지 않았다.", verdict.touched_paths
        )

    if result.returncode == 0:
        return PatchVerdict(True, touched_paths=verdict.touched_paths)
    # 오류 메시지는 재생성 요청에 그대로 붙인다. 무엇이 어긋났는지 알려야 모델이 고친다.
    message = (result.stderr or result.stdout).strip()
    return PatchVerdict(False, "patch_does_not_apply", message, verdict.touched_paths)
