import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.agents import source_context
from app.models import AgentState, JobStatus, RepositoryValidationRequest, SandboxResult
from app.repository.validation_configuration import configuration_text, overlay_configuration
from app.sandbox import docker_runner
from app.sandbox.docker_runner import SandboxRunner
from app.workflow.repository_validation import (create_validation_state, _queue_payload,
    _state_from_queue_payload, _sandbox_contract_options, _offer_replica_override)

TEXT = 'version: 1\nservice: {port: 8000, replicas: 1}\n'


class Response:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self): return json.dumps(self.body).encode()


class ValidationUploadTests(unittest.TestCase):
    def runner(self, **options):
        result = SandboxRunner()
        result.settings = SimpleNamespace(sandbox_base_url='http://sandbox:8100',
            sandbox_repository_path='/repositories/validate', sandbox_http_timeout_seconds=600,
            local_sandbox_enabled=False, **options)
        return result

    def test_server_and_ai_queue_contracts_keep_the_upload(self):
        for payload in [dict(taskId='one', repositoryUrl='https://github.com/example/app', validationYaml=TEXT),
                        dict(type='repository_validation', job_id='one', repository_url='https://github.com/example/app', validation_yaml=TEXT)]:
            state = _state_from_queue_payload(payload)
            self.assertEqual(state.validation_yaml, TEXT)
            self.assertEqual(_queue_payload(state)['validationYaml'], TEXT)
            self.assertEqual(_sandbox_contract_options(state)['validation_yaml'], TEXT)

    def test_previous_calls_do_not_receive_a_new_none_keyword(self):
        state = AgentState(job_id='one', repository_url='https://github.com/example/app')
        self.assertNotIn('validation_yaml', _sandbox_contract_options(state))
        self.assertNotIn('validationYaml', _queue_payload(state))

    def test_transport_normalizes_and_bounds_utf8_input(self):
        request = RepositoryValidationRequest(repository_url='https://github.com/example/app', validation_yaml='\ufeff' + TEXT.replace('\n', '\r\n'))
        self.assertEqual(create_validation_state(request).validation_yaml, TEXT)
        for raw in [' ', '가' * 22000, 'a\0b']:
            with self.subTest(raw=raw[:10]), self.assertRaises(ValueError):
                configuration_text(raw)

    def test_http_request_preserves_config_and_rejects_an_old_sandbox_that_ignores_it(self):
        digest = hashlib.sha256(TEXT.encode()).hexdigest()
        for actual, code in [(digest, None), (None, 'uploaded_configuration_not_applied'), ('b' * 64, 'uploaded_configuration_not_applied')]:
            response = {'exitCode': 0, 'observationStatus': 'observed', 'source': {
                'configuration_input': {'uploaded_sha256': actual}}}
            with patch.object(docker_runner, 'urlopen', return_value=Response(response)) as opener:
                result = self.runner().run_repository('https://github.com/example/app', validation_yaml=TEXT)
            self.assertEqual(json.loads(opener.call_args.args[0].data)['validationYaml'], TEXT)
            self.assertEqual(result.infra_error, code)
            self.assertEqual(result.exit_code, 0 if code is None else None)

    def test_an_uploaded_config_does_not_silently_use_local_docker(self):
        runner = self.runner()
        runner.settings.sandbox_base_url = None
        result = runner.run_repository('https://github.com/example/app', validation_yaml=TEXT)
        self.assertEqual(result.infra_error, 'sandbox_configuration_unsupported')
        self.assertIsNone(result.exit_code)

    def test_context_uses_uploaded_file_then_the_refinement_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            (root / 'app.py').write_text('print("hello")\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=test@localhost',
                            'commit', '-qm', 'test: source'], check=True, capture_output=True)
            diff = ('diff --git a/.codereferee/validation.yaml b/.codereferee/validation.yaml\n'
                    '--- a/.codereferee/validation.yaml\n+++ b/.codereferee/validation.yaml\n'
                    '@@ -1,2 +1,2 @@\n version: 1\n-service: {port: 8000, replicas: 1}\n+service: {port: 8000, replicas: 2}\n')
            files = source_context.collect(str(root), ['app.py'], validation_yaml=TEXT, applied_patch=diff)
            self.assertIn('replicas: 2', files['.codereferee/validation.yaml'])
            self.assertFalse((root / '.codereferee').exists())

    def test_missing_source_context_never_offers_a_new_config_over_an_upload(self):
        state = AgentState(job_id='one', repository_url='https://github.com/example/app', validation_yaml=TEXT,
            execution_result=SandboxResult(exit_code=0, chaos_observation={'type': 'container_kill',
                'target_configuration': {'replicas': 1}}), status=JobStatus.failed)
        _offer_replica_override(state)
        self.assertEqual(state.source_files, {})
        self.assertNotIn('new_source_files', state.metrics)


if __name__ == '__main__':
    unittest.main()
