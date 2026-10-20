import subprocess
import tempfile
import unittest
from pathlib import Path

from app.agents import source_context

# 실제 sandbox 로그. 합성 로그로 만들면 형태가 어긋난다(evaluation-design 14.3의 교훈).
COMPILEALL_LOG = """Listing './.git/refs/tags'...
Listing './documentation'...
Compiling './documentation/conf.py'...
***   File "./documentation/conf.py", line 219
    def broken(:
               ^
SyntaxError: invalid syntax

Compiling './setup.py'...
Compiling './six.py'...
"""

COMPILEALL_LOG_WITH_PREAMBLE = """[CodeReferee] installing sandbox clone tools
apt-get install -y --no-install-recommends git ca-certificates
debconf: unable to initialize frontend: Dialog
[CodeReferee] cloning repository
Cloning into '/tmp/repository'...
[CodeReferee] resolving commit
a1b2c3d
[CodeReferee] detecting project stack
detected_stack=python
""" + COMPILEALL_LOG

PYTEST_LOG = """============================= FAILURES ==============================
_________________________ test_add _________________________
tests/test_calc.py:14: in test_add
    assert add(1, 2) == 4
E   AssertionError
=========================== short test summary info ===========================
FAILED tests/test_calc.py::test_add - AssertionError
"""

TRACEBACK_LOG = """Traceback (most recent call last):
  File "/tmp/repository/app/main.py", line 42, in handler
    return client.get(url)
  File "/usr/lib/python3.12/http/client.py", line 1000, in get
ConnectionError: refused
"""


class PathExtractionTests(unittest.TestCase):
    """로그에서 고쳐야 할 파일 경로를 뽑는다. Refiner가 파일을 못 보면 diff를 쓸 수 없다."""

    def test_compileall_error_path_is_found_and_normalised(self) -> None:
        paths = source_context.extract_paths(COMPILEALL_LOG)
        self.assertEqual(paths[0], "documentation/conf.py")

    def test_files_that_merely_compiled_are_not_prioritised(self) -> None:
        # setup.py와 six.py는 성공한 파일이다. 실패한 파일이 먼저 와야 한다.
        paths = source_context.extract_paths(COMPILEALL_LOG)
        self.assertLess(paths.index("documentation/conf.py"), len(paths))
        self.assertEqual(paths[0], "documentation/conf.py")

    def test_pytest_failure_path_is_found(self) -> None:
        self.assertIn("tests/test_calc.py", source_context.extract_paths(PYTEST_LOG))

    def test_repository_paths_are_kept_and_interpreter_paths_dropped(self) -> None:
        paths = source_context.extract_paths(TRACEBACK_LOG)
        self.assertIn("app/main.py", paths)
        # 표준 라이브러리는 사용자 레포 파일이 아니다.
        self.assertFalse(any("http/client.py" in p for p in paths))

    def test_git_internals_are_never_returned(self) -> None:
        paths = source_context.extract_paths(COMPILEALL_LOG)
        self.assertFalse(any(p.startswith(".git/") for p in paths))

    def test_result_is_capped(self) -> None:
        log = "\n".join(f'File "./mod{i}.py", line 1' for i in range(20))
        self.assertLessEqual(len(source_context.extract_paths(log)), source_context.MAX_FILES)

    def test_no_paths_in_the_log_is_not_an_error(self) -> None:
        self.assertEqual(source_context.extract_paths("Segmentation fault"), [])


class SourceReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "app").mkdir()
        (self.repo / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "huge.py").write_text("# pad\n" * 40_000, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_requested_file_is_read(self) -> None:
        files = source_context.read_files(self.repo, ["app/main.py"])
        self.assertEqual(files["app/main.py"], "x = 1\n")

    def test_oversized_file_is_skipped_not_truncated(self) -> None:
        # 잘라서 주면 context 줄이 어긋나 적용 불가능한 diff가 나온다. 아예 주지 않는다.
        files = source_context.read_files(self.repo, ["huge.py"])
        self.assertNotIn("huge.py", files)

    def test_missing_file_is_skipped(self) -> None:
        self.assertEqual(source_context.read_files(self.repo, ["nope.py"]), {})

    def test_path_escaping_the_repository_is_refused(self) -> None:
        self.assertEqual(source_context.read_files(self.repo, ["../outside.py"]), {})


class WorkflowWiringTests(unittest.TestCase):
    """판정이 Fail일 때만 파일을 모은다. 통과한 검증에는 고칠 것이 없다."""

    def _state(self, status):
        from app.models import AgentState, JobStatus, SandboxResult

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=status)
        state.execution_result = SandboxResult(exit_code=1, stderr=COMPILEALL_LOG)
        return state

    def test_passing_validation_does_not_clone(self) -> None:
        from unittest import mock

        from app.models import JobStatus
        from app.workflow import repository_validation as workflow

        with mock.patch.object(source_context, "collect") as collect:
            workflow.attach_source_files(self._state(JobStatus.success))
        collect.assert_not_called()

    def test_failing_validation_collects_the_failing_file(self) -> None:
        from unittest import mock

        from app.models import JobStatus
        from app.workflow import repository_validation as workflow

        state = self._state(JobStatus.failed)
        with mock.patch.object(
            source_context, "collect", return_value={"documentation/conf.py": "x = 1\n"}
        ) as collect:
            workflow.attach_source_files(state)
        self.assertEqual(collect.call_args.args[1][0], "documentation/conf.py")
        self.assertIn("documentation/conf.py", state.source_files)

    def test_evidence_packet_carries_the_source_files(self) -> None:
        from app.agents.evidence import build_evidence_packet
        from app.models import JobStatus

        state = self._state(JobStatus.failed)
        state.source_files = {"documentation/conf.py": "x = 1\n"}
        self.assertEqual(build_evidence_packet(state)["source_files"], state.source_files)

    def test_source_files_do_not_reach_the_backend_result_event(self) -> None:
        from app import events
        from app.models import JobStatus

        state = self._state(JobStatus.failed)
        state.source_files = {"documentation/conf.py": "x = 1\n"}
        # 소스 전문이 응답 페이로드로 나가면 안 된다.
        self.assertNotIn("source_files", events.result_event(state))


class RefinerEditsToDiffTests(unittest.TestCase):
    """모델은 바꿀 줄만 준다. 치환은 우리가 하므로 다른 부분이 바뀔 수 없다."""

    def _state(self):
        from app.models import AgentState, JobStatus

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.source_files = {"calc.py": "def add(a, b):\n    return a - b\n"}
        return state

    def test_edit_becomes_a_diff(self) -> None:
        from app.agents import nodes

        state = self._state()
        state.refiner_report = {
            "summary": "fix sign",
            "edits": [{"path": "calc.py", "find": ["    return a - b"], "replace": ["    return a + b"]}],
            "patch_guidance": ["fix"],
            "verification_steps": ["run tests"],
            "risk": "low",
        }
        nodes._diff_from_edits(state)
        self.assertIn("-    return a - b", state.refiner_report["patch_diff"])
        self.assertIn("+    return a + b", state.refiner_report["patch_diff"])
        self.assertEqual(state.refiner_report["patched_paths"], ["calc.py"])
        self.assertNotIn("edits", state.refiner_report)

    def test_edit_on_a_file_we_never_showed_is_rejected(self) -> None:
        from app.agents import nodes

        state = self._state()
        state.refiner_report = {"edits": [{"path": "secret.py", "find": ["x = 1"], "replace": ["x = 2"]}]}
        nodes._diff_from_edits(state)
        self.assertEqual(state.metrics["patch_check"]["reason_code"], "edits_not_applicable")
        self.assertTrue(any("edit_path_unknown" in e for e in state.events))

    def test_anchor_that_does_not_exist_is_rejected(self) -> None:
        from app.agents import nodes

        state = self._state()
        state.refiner_report = {"edits": [{"path": "calc.py", "find": ["nonexistent line"], "replace": ["x"]}]}
        nodes._diff_from_edits(state)
        self.assertTrue(any("edit_anchor_not_found" in e for e in state.events))

    def test_no_edits_leaves_the_report_alone(self) -> None:
        from app.agents import nodes

        state = self._state()
        state.refiner_report = {"summary": "no fix", "patch_diff": None}
        nodes._diff_from_edits(state)
        self.assertIsNone(state.refiner_report["patch_diff"])


class ManifestAttachmentTests(unittest.TestCase):
    """의존성 실패 로그에는 파일 경로가 없다. pip은 패키지 이름만 말한다."""

    def _state(self, category: str, log: str):
        from app.models import AgentState, JobStatus, SandboxResult

        state = AgentState(job_id="t", repository_url="https://github.com/o/r", status=JobStatus.failed)
        state.execution_result = SandboxResult(exit_code=1, stderr=log)
        state.judge_report = {"reason_category": category}
        return state

    def test_dependency_failure_asks_for_the_manifest(self) -> None:
        from unittest import mock

        from app.workflow import repository_validation as workflow

        log = "[CodeReferee] detecting project stack\nERROR: No matching distribution found for nope-zzz\n"
        with mock.patch.object(source_context, "collect", return_value={}) as collect:
            workflow.attach_source_files(self._state("dependency_install_failed", log))
        requested = collect.call_args.args[1]
        self.assertIn("requirements.txt", requested)

    def test_source_failure_keeps_using_the_log_paths(self) -> None:
        from unittest import mock

        from app.workflow import repository_validation as workflow

        with mock.patch.object(source_context, "collect", return_value={}) as collect:
            workflow.attach_source_files(self._state("sandbox_nonzero_exit", COMPILEALL_LOG))
        self.assertEqual(collect.call_args.args[1], ["documentation/conf.py"])


class CaretLineTests(unittest.TestCase):
    """캐럿 줄은 열 위치 표시다. 파일에 없는 줄이라 앵커로 쓰면 반드시 거부된다."""

    def test_caret_only_line_is_dropped(self) -> None:
        log = 'File "./a.py", line 3\n    def broken(:\n               ^\nSyntaxError: invalid syntax\n'
        cleaned = source_context.strip_caret_lines(log)
        self.assertNotIn("               ^", cleaned)
        self.assertIn("def broken(:", cleaned)
        self.assertIn("SyntaxError", cleaned)

    def test_a_line_containing_a_caret_in_code_is_kept(self) -> None:
        # 비트 XOR 연산자가 있는 코드 줄은 지우면 안 된다.
        log = "    checksum = a ^ b\n"
        self.assertIn("checksum = a ^ b", source_context.strip_caret_lines(log))
