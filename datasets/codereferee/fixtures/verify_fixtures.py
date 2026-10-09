"""fixture 매트릭스 자동 검증.

두 가지를 확정한다(실제 샌박 코드로):
1. 배포 플랜: execution_plan.resolve_plan 이 각 fixture를 LABEL의 expected_plan_source 로
   해석하는가 (yaml 유무 포함). = "샌박에서 실행된다"의 정적 증명.
2. 결함 존재: LABEL.defect_probe 가 선언한 결함이 실제로 코드에 있는가 (단위 수준).

런타임 결과(HTTP 500, chaos 판정, 롤아웃 실패)는 full-stack 실행이 필요해 여기서 검증하지
않는다 — LABEL.unverified_runtime 에 명시돼 있다.

실행:
  <sbx venv>/bin/python verify_fixtures.py
"""
import importlib.util
import json
import pathlib
import subprocess
import sys

SBX = "/private/tmp/claude-501/-Users-jeongseung-yun-projects-codereferee/b589c724-9686-4f6b-853c-50a4f60ef337/scratchpad/sbx-repo/scripts"
sys.path.insert(0, SBX)
import execution_plan as ep  # noqa: E402

HERE = pathlib.Path(__file__).parent


def check_plan(repo: pathlib.Path, label: dict) -> tuple[bool, str]:
    expected = label["expected_plan_source"]
    try:
        plan = ep.resolve_plan(repo)
    except ep.ConfigurationRequired as e:
        ok = expected == "ConfigurationRequired"
        return ok, f"ConfigurationRequired: {e}"
    got = plan.get("source")
    return got == expected, f"source={got} port={plan.get('port')} replicas={plan.get('replicas')}"


def check_defect(repo: pathlib.Path, label: dict) -> tuple[bool, str]:
    probe = label.get("defect_probe")
    if not probe:
        return True, "defect=none"
    if "callable" in probe:
        mod_path = repo / (probe["module"] + ".py")
        spec = importlib.util.spec_from_file_location(f"fx_{label['id']}", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, probe["callable"])
        try:
            fn(probe["arg"])
            return False, f"{probe['callable']} did NOT raise (expected {probe['raises']})"
        except Exception as e:  # noqa: BLE001
            ok = type(e).__name__ == probe["raises"]
            return ok, f"raised {type(e).__name__}"
    if "run" in probe:
        cmd = probe["run"].split()
        if cmd and cmd[0] == "python":
            cmd[0] = sys.executable
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        ok = (proc.returncode != 0) == probe.get("expect_nonzero_exit", True)
        return ok, f"exit={proc.returncode}"
    return True, "no-op"


def main() -> int:
    rows = []
    all_ok = True
    for label_file in sorted(HERE.glob("*/LABEL.json")):
        repo = label_file.parent
        label = json.loads(label_file.read_text(encoding="utf-8"))
        yaml_present = (repo / ".codereferee/validation.yaml").is_file()
        plan_ok, plan_msg = check_plan(repo, label)
        defect_ok, defect_msg = check_defect(repo, label)
        ok = plan_ok and defect_ok
        all_ok &= ok
        rows.append((("✅" if ok else "❌"), label["id"], f"yaml={'Y' if yaml_present else 'N'}",
                     label["deploy_shape"], plan_msg, defect_msg))

    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))
    print("\n" + ("ALL PASS" if all_ok else "FAILURES ABOVE"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
