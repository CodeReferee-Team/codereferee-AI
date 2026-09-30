import json
import tempfile
import unittest
from pathlib import Path

from app.repository import stack_detection as sd


def _repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


class StackDetectionTests(unittest.TestCase):
    """스택은 매니페스트로 가른다. sandbox가 clone 후에 하던 일을 앞으로 당긴다."""

    def test_node_is_detected_by_package_json(self) -> None:
        self.assertEqual(sd.detect_stack(_repo({"package.json": "{}"})), "node")

    def test_python_is_detected_by_any_manifest(self) -> None:
        for manifest in ("pyproject.toml", "setup.py", "requirements.txt"):
            self.assertEqual(sd.detect_stack(_repo({manifest: ""})), "python")

    def test_gradle_and_maven(self) -> None:
        self.assertEqual(sd.detect_stack(_repo({"build.gradle": ""})), "gradle")
        self.assertEqual(sd.detect_stack(_repo({"pom.xml": ""})), "maven")

    def test_repository_without_a_manifest_is_unknown(self) -> None:
        self.assertEqual(sd.detect_stack(_repo({"README.md": "hi"})), "unknown")


class RunCommandTests(unittest.TestCase):
    """실행 커맨드가 없으면 띄울 서비스가 없다는 뜻이다. 카오스 대상 판별에 쓴다."""

    def test_python_uvicorn_layouts(self) -> None:
        self.assertEqual(
            sd.detect_run_command(_repo({"requirements.txt": "", "app/main.py": ""}), "python"),
            sd.RunTarget(["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"], 8000),
        )
        self.assertEqual(
            sd.detect_run_command(_repo({"requirements.txt": "", "main.py": ""}), "python").port, 8000
        )

    def test_django_manage_py(self) -> None:
        target = sd.detect_run_command(_repo({"requirements.txt": "", "manage.py": ""}), "python")
        self.assertIn("runserver", target.command)

    def test_flask_app_py(self) -> None:
        target = sd.detect_run_command(_repo({"requirements.txt": "", "app.py": ""}), "python")
        self.assertIn("flask", target.command)

    def test_node_start_script_is_preferred(self) -> None:
        pkg = json.dumps({"scripts": {"dev": "vite", "start": "node server.js"}})
        target = sd.detect_run_command(_repo({"package.json": pkg}), "node")
        self.assertEqual(target.command, ["npm", "run", "start"])
        self.assertEqual(target.port, 3000)

    def test_node_without_a_server_script_has_no_run_target(self) -> None:
        pkg = json.dumps({"scripts": {"test": "jest"}})
        self.assertIsNone(sd.detect_run_command(_repo({"package.json": pkg}), "node"))

    def test_spring_boot_is_required_for_gradle_and_maven(self) -> None:
        plain = _repo({"build.gradle": "plugins { id 'java' }", "gradlew": ""})
        self.assertIsNone(sd.detect_run_command(plain, "gradle"))
        boot = _repo({"build.gradle": "id 'org.springframework.boot'", "gradlew": ""})
        self.assertIn("bootRun", sd.detect_run_command(boot, "gradle").command)

    def test_a_library_has_no_run_target(self) -> None:
        # six, iniconfig 같은 라이브러리는 띄울 서비스가 없다. 카오스 검증 대상이 아니다.
        self.assertIsNone(sd.detect_run_command(_repo({"setup.py": "", "six.py": ""}), "python"))

    def test_host_is_bindable_from_outside_the_container(self) -> None:
        # 127.0.0.1로 바인딩하면 Pod 밖에서 probe할 수 없다. 삭제된 코드는 로컬 프로세스 기준이었다.
        target = sd.detect_run_command(_repo({"requirements.txt": "", "main.py": ""}), "python")
        self.assertIn("0.0.0.0", target.command)
        self.assertNotIn("127.0.0.1", " ".join(target.command))


class ProjectProfileTests(unittest.TestCase):
    def test_profile_reports_commands_and_service_flag(self) -> None:
        profile = sd.profile(_repo({"requirements.txt": "", "app/main.py": "", "tests/test_x.py": ""}))
        self.assertEqual(profile.stack, "python")
        self.assertTrue(profile.is_service)
        self.assertIsNotNone(profile.test_command)

    def test_library_profile_is_not_a_service(self) -> None:
        profile = sd.profile(_repo({"setup.py": "", "six.py": ""}))
        self.assertFalse(profile.is_service)


class MixedManifestTests(unittest.TestCase):
    """package.json이 있어도 서버 스택이라는 뜻은 아니다. 실측에서 Django가 node로 잡혔다."""

    def test_python_project_with_tooling_package_json_is_python(self) -> None:
        # Django 레포의 실제 구성. scripts는 grunt/biome 도구만 담는다.
        repo = _repo({
            "pyproject.toml": "",
            "package.json": json.dumps({"scripts": {"test": "grunt test", "biome": "biome check"}}),
        })
        self.assertEqual(sd.detect_stack(repo), "python")

    def test_node_server_scripts_still_win(self) -> None:
        repo = _repo({
            "pyproject.toml": "",
            "package.json": json.dumps({"scripts": {"start": "node server.js"}}),
        })
        self.assertEqual(sd.detect_stack(repo), "node")

    def test_package_json_without_scripts_falls_back_to_node(self) -> None:
        self.assertEqual(sd.detect_stack(_repo({"package.json": "{}"})), "node")
