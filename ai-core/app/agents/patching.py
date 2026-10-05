"""모델이 낸 수정안을 diff로 바꾸고, sandbox로 보내기 전에 검사한다.

- apply_edits: 모델이 지목한 줄만 치환한다. 지목하지 않은 줄은 바뀔 수 없다.
- build_diff: 치환 결과에서 diff를 만든다. context 줄과 hunk 헤더가 틀릴 수 없다.
- inspect_diff: 크기 상한, 보호 경로, 레포 밖 경로를 막는다.
- inspect_rewrite: 수리인지 재작성인지 본다.

우리가 실행할 패치이므로 CI 설정이나 레포 밖 경로를 건드리는 변경은 애초에 들여보내면 안 된다.
적용 가능성 자체는 sandbox가 clone 직후 `git apply --check`로 확인한다(docker_runner의 patch 단계).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

# 수정 패치가 지울 수 있는 줄의 상한. 이보다 크면 수리가 아니라 재작성이다.
# 작은 모델은 파일 전문을 재현하라고 하면 일부를 조용히 빠뜨린다(7,038자 -> 4,567자를 관측).
# 그렇게 만들어진 diff는 실제 내용에서 뽑은 것이라 git apply를 통과하므로 여기서 막아야 한다.
# 실측으로 정한 값. 221줄 파일에서 32줄을 지우고 문법 오류 한 줄만 고친 패치가 20% 상한을
# 통과해 "고쳤다"로 기록됐다. 재실행도 통과했다(compileall은 지워진 설정을 보지 않는다).
# 수리는 국소적이어야 한다.
MAX_REMOVED_LINE_RATIO = 0.05
MAX_REMOVED_LINES_FLOOR = 10

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




def build_diff(original: dict[str, str], patched: dict[str, str]) -> str:
    """고친 파일 전문에서 unified diff를 만든다.

    모델에게 diff를 쓰게 하면 context 줄과 hunk 헤더를 틀린다(docs/evaluation-design.md 14.5).
    전문을 받아 우리가 만들면 그 둘이 틀릴 수 없다.

    original에 없는 경로는 무시한다. 우리가 보여준 파일만 고칠 수 있다.
    """
    chunks: list[str] = []
    for path in sorted(patched):
        before = original.get(path)
        after = patched[path]
        if before is None or not isinstance(after, str) or before == after:
            continue
        # difflib은 마지막 줄의 개행 유무를 표시하지 못한다. 양쪽을 같은 규칙으로 맞춘다.
        if not before.endswith("\n"):
            before += "\n"
        if not after.endswith("\n"):
            after += "\n"
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        chunks.append("".join(diff))
    return "".join(chunks)


def inspect_rewrite(diff: str, original: dict[str, str]) -> PatchVerdict:
    """수리인지 재작성인지 본다.

    모델이 파일 전문을 다 쓰지 못하고 뒤를 잘라먹으면, 그 내용으로 만든 diff는 멀쩡한 코드를
    대량 삭제하는 패치가 된다. 실제 내용에서 뽑았으므로 `git apply`는 통과한다. 크기로 막는다.
    """
    removed_by_path: dict[str, int] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("--- "):
            current = line[4:].strip()
            if current.startswith("a/"):
                current = current[2:]
        elif line.startswith("-") and not line.startswith("---"):
            removed_by_path[current] = removed_by_path.get(current, 0) + 1

    for path, removed in removed_by_path.items():
        total = len(original.get(path, "").splitlines()) or 1
        allowed = max(MAX_REMOVED_LINES_FLOOR, int(total * MAX_REMOVED_LINE_RATIO))
        if removed > allowed:
            return PatchVerdict(
                False,
                "patch_rewrites_file",
                f"{path}에서 {removed}줄을 지운다. 전체 {total}줄 기준 상한 {allowed}줄을 넘는다.",
                sorted(removed_by_path),
            )
    return PatchVerdict(True, touched_paths=sorted(removed_by_path))


@dataclass
class EditOutcome:
    patched: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)


def _normalise_edit_path(path: str) -> str:
    cleaned = path.strip()
    if cleaned.startswith("/tmp/repository/"):
        cleaned = cleaned[len("/tmp/repository/") :]
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def apply_edits(original: dict[str, str], edits: list[dict[str, object]]) -> EditOutcome:
    """내용으로 앵커한 편집을 적용한다.

    `find`가 파일에 정확히 한 번 나타날 때만 바꾼다. 여러 번이면 어디를 말하는지 알 수 없고,
    없으면 모델이 본 적 없는 내용을 지어낸 것이다. 둘 다 거부한다.
    치환은 우리가 하므로 파일의 다른 부분은 바뀔 수 없다.
    """
    outcome = EditOutcome(patched=dict())
    working = dict(original)
    # 모델이 "./src/a.py"나 "/tmp/repository/src/a.py"처럼 쓴다. 로그에서 본 형태를 그대로 옮기는 것이다.
    lookup = {_normalise_edit_path(key): key for key in working}
    for index, edit in enumerate(edits):
        path = str(edit.get("path") or "")
        find = edit.get("find") or []
        replace = edit.get("replace")
        replace = replace if isinstance(replace, list) else []
        resolved = path if path in working else lookup.get(_normalise_edit_path(path), "")
        if not resolved:
            outcome.rejected.append(f"edit_path_unknown:{path or f'#{index}'}")
            continue
        path = resolved
        if not isinstance(find, list) or not find:
            outcome.rejected.append(f"edit_anchor_empty:{path}")
            continue

        content = working[path]
        anchor = "\n".join(str(line) for line in find)
        count = content.count(anchor)
        if count == 0:
            outcome.rejected.append(f"edit_anchor_not_found:{path}")
            continue
        if count > 1:
            outcome.rejected.append(f"edit_anchor_ambiguous:{path}")
            continue
        working[path] = content.replace(anchor, "\n".join(str(line) for line in replace), 1)

    outcome.patched = {path: text for path, text in working.items() if text != original.get(path)}
    return outcome
