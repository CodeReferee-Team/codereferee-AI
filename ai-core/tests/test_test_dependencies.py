import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from app.sandbox import docker_runner

WHEEL = "wheels/cr_test_helper-1.0-py3-none-any.whl"


def _write_wheel(path: str) -> None:
    """네트워크 없이 설치할 수 있는 가장 작은 wheel. 빌드 도구가 필요 없다."""
    files = {
        "cr_test_helper.py": "VALUE = 42\n",
        "cr_test_helper-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: cr-test-helper\nVersion: 1.0\n",
        "cr_test_helper-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for name, body in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={digest},{len(body)}")
    record.append("cr_test_helper-1.0.dist-info/RECORD,,")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as wheel:
        for name, body in files.items():
            wheel.writestr(name, body)
        wheel.writestr("cr_test_helper-1.0.dist-info/RECORD", "\n".join(record) + "\n")


USES_HELPER = "from cr_test_helper import VALUE\n\n\ndef test_helper():\n    assert VALUE == 42\n"


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git과 sh가 필요하다")
class ConventionalTestDependencyTests(unittest.TestCase):
    """선언이 없으면 흔한 자리에서 테스트 의존성을 찾는다. 정상 레포 30개 중 14개가 이것 때문에 판정 불가였다."""

    def _run(self, files: dict[str, str]) -> tuple[dict, str]:
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        source = os.path.join(root, "source")
        for path, body in {"requirements.txt": "", **files}.items():
            full = os.path.join(source, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(body)
        _write_wheel(os.path.join(source, WHEEL))
        git = ["git", "-C", source, "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)

        # 설치가 이 테스트를 돌리는 Python을 더럽히지 않게 따로 venv를 둔다. pytest는 바깥 것을 쓴다.
        venv = os.path.join(root, "venv")
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", venv], check=True)
        env = dict(os.environ, PATH=os.path.join(venv, "bin") + os.pathsep + os.environ["PATH"])
        script = docker_runner._repository_validation_script(f"file://{source}", None, None)
        script = script.replace("/tmp/repository", os.path.join(root, "clone"))
        completed = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=300, env=env)
        line = next(l for l in completed.stdout.splitlines() if l.startswith(docker_runner.RESULT_SENTINEL))
        return json.loads(line[len(docker_runner.RESULT_SENTINEL):]), completed.stdout + completed.stderr

    def test_dependency_group_is_installed(self) -> None:
        result, log = self._run({
            "pyproject.toml": f'[dependency-groups]\ntests = ["./{WHEEL}"]\n',
            "tests/test_x.py": USES_HELPER,
        })
        self.assertIn("test_dependencies=dependency-groups.tests", log)
        self.assertEqual(result["outcome"], "success")

    def test_included_groups_are_followed(self) -> None:
        result, _ = self._run({
            "pyproject.toml": f'[dependency-groups]\nbase = ["./{WHEEL}"]\ndev = [{{include-group = "base"}}]\n',
            "tests/test_x.py": USES_HELPER,
        })
        self.assertEqual(result["outcome"], "success")

    def test_optional_dependency_extra_is_installed(self) -> None:
        # 패키지 빌드에는 네트워크가 필요해 extra 자체는 설치 명령만 확인한다.
        script = docker_runner._repository_validation_script("https://github.com/o/r", None, None)
        self.assertIn('.[${_kind#extra }]', script)

    def test_requirements_file_is_installed(self) -> None:
        result, log = self._run({"requirements/tests.txt": f"./{WHEEL}\n", "tests/test_x.py": USES_HELPER})
        self.assertIn("test_dependencies=requirements/tests.txt", log)
        self.assertEqual(result["outcome"], "success")

    def test_a_guessed_install_that_fails_is_not_a_dependency_failure(self) -> None:
        # 우리가 추측한 자리다. 실패해도 레포 탓이 아니므로 의존성 단계를 실패시키지 않는다.
        result, _ = self._run({
            "pyproject.toml": '[dependency-groups]\ntests = ["./wheels/missing-1.0-py3-none-any.whl"]\n',
            "tests/test_x.py": USES_HELPER,
        })
        self.assertEqual(result["failed_step"], "smoke")
        self.assertEqual(result["exit_code"], 2)  # 수집 오류. 선언이 없으니 판정 불가로 간다.

    def test_a_declared_test_dependency_file_wins(self) -> None:
        result, log = self._run({
            ".codereferee/validation.yaml": "testDependencies: requirements/ci.txt\n",
            "requirements/ci.txt": f"./{WHEEL}\n",
            "requirements/tests.txt": "./wheels/missing-1.0-py3-none-any.whl\n",
            "tests/test_x.py": USES_HELPER,
        })
        self.assertNotIn("test_dependencies=", log)
        self.assertEqual(result["outcome"], "success")


if __name__ == "__main__":
    unittest.main()
