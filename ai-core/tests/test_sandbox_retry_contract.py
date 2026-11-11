import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings
from app.sandbox.docker_runner import SandboxRunner
from test_sandbox_deadline import _Response


class SandboxRetryContractTests(unittest.TestCase):
    def payload(self, **settings):
        runner = SandboxRunner()
        runner.settings = Settings(_env_file=None, sandbox_base_url='http://sandbox:8100', **settings)
        with patch('app.sandbox.docker_runner.urlopen', return_value=_Response()) as opener:
            runner.run_repository('https://github.com/example/project', request_id='stable-id', patch_diff='patch')
        self.assertEqual(opener.call_args.kwargs['timeout'], runner.settings.sandbox_http_timeout_seconds)
        return json.loads(opener.call_args.args[0].data)

    def test_defaults_preserve_existing_request(self):
        payload = self.payload()
        for key in ('preparationTimeoutSeconds', 'maxPreparationRetries', 'retryDownloads', 'phaseTimeouts'):
            self.assertNotIn(key, payload)
        self.assertEqual(payload['deadlineSeconds'], 570)

    def test_policy_is_forwarded_without_changing_identity_or_patch(self):
        payload = self.payload(sandbox_http_timeout_seconds=510, sandbox_preparation_timeout_seconds=300,
            sandbox_preparation_max_retries=1, sandbox_retry_downloads=True, sandbox_phase_timeouts={'install': 12})
        self.assertEqual(payload['deadlineSeconds'], 480)
        self.assertEqual(payload['preparationTimeoutSeconds'], 300)
        self.assertEqual(payload['maxPreparationRetries'], 1)
        self.assertTrue(payload['retryDownloads'])
        self.assertEqual(payload['phaseTimeouts'], {'install': 12})
        self.assertEqual(payload['requestId'], 'stable-id')
        self.assertEqual(payload['patchDiff'], 'patch')

    def test_invalid_policy_is_rejected(self):
        for settings in ({'sandbox_preparation_max_retries': 2},
                         {'sandbox_phase_timeouts': {'unknown': 10}},
                         {'sandbox_phase_timeouts': {'install': True}},
                         {'sandbox_phase_timeouts': {'build': 7201}}):
            with self.subTest(settings=settings), self.assertRaises(ValidationError):
                Settings(_env_file=None, **settings)
