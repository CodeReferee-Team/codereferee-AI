import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app.agents import nodes, source_context
from app.agents.evidence import build_refiner_evidence, build_llm_evidence_packet
from app.agents.repository_sources import target_repository
from app.config import get_settings
from app.models import AgentState, JobStatus, RepositoryPreflightReport, SandboxResult
from app.workflow import repository_validation as workflow


MAIN_SHA = 'a' * 40
BACKEND_SHA = 'b' * 40
DIFF = '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n'


def result(recovered=False):
    return SandboxResult(exit_code=0, observation_status='observed',
        baseline={'metrics': {'availability': 1.0}},
        metrics={'availability': 1.0, 'error_rate': 0.0, 'probe_count': 5},
        chaos_observation={'type': 'dependency_database_outage', 'recovered': recovered,
                           'recovery_seconds': 2, 'target_configuration': {'replicas': 2}},
        source={'repositories': {'main': {'url': 'https://github.com/o/frontend.git', 'commitSha': MAIN_SHA},
                                 'backend': {'url': 'https://github.com/o/backend.git', 'commitSha': BACKEND_SHA}},
                'service_repositories': {'web': 'main', 'api': 'backend'},
                'fault_target': {'deployment': 'cr-svc-api'}})


def state():
    value = AgentState(job_id='job', repository_url='https://github.com/o/frontend.git', status=JobStatus.failed)
    value.preflight_report = RepositoryPreflightReport(repository_url=value.repository_url,
        cloneable=True, executable=True, resolved_commit_sha=MAIN_SHA)
    value.execution_result = result()
    value.judge_report = {'status': 'Fail', 'reason_category': 'chaos_not_recovered'}
    return value


class RepositorySourceTests(unittest.TestCase):
    def test_critic_prompt_distinguishes_shared_dependency_faults(self):
        from app.agents.prompts import CRITIC_PROMPT
        self.assertIn('dependency_redis_outage', CRITIC_PROMPT)
        self.assertIn('dependency_database_outage', CRITIC_PROMPT)
        self.assertIn('shared dependency', CRITIC_PROMPT)

    def test_structured_build_error_reaches_context_and_model(self):
        value = state()
        value.execution_result.chaos_observation = {}
        value.execution_result.stderr = 'Repository verification failed.'
        value.execution_result.sandbox_report = {
            'failed_repository': 'backend', 'failed_step': 'build',
            'steps': [{'name': 'build', 'exit_code': 1,
                       'stdout': "*** Error compiling './app.py'...\nIndentationError: expected an indented block (app.py, line 18)",
                       'stderr': ''}],
        }
        with patch.object(source_context, 'collect', return_value={'app.py': 'patched\n'}) as collect:
            workflow.attach_source_files(value, repository_patches={
                'backend': {'commitSha': BACKEND_SHA, 'patchDiff': DIFF}})
        self.assertIn('app.py', collect.call_args.args[1])
        self.assertEqual(collect.call_args.kwargs['applied_patch'], DIFF)
        self.assertIn('IndentationError', build_llm_evidence_packet(value)['failure_log'])
        self.assertIn('IndentationError', build_refiner_evidence(value)['execution']['log_excerpt'])

    def test_step_log_ignores_success_and_malformed_entries(self):
        value = SandboxResult(exit_code=1, sandbox_report={'failed_step': 'build', 'steps': [
            None, {'name': 'install', 'stdout': 'unrelated success'},
            {'name': 'build', 'stdout': {'invalid': 'shape'}, 'stderr': 'actual build failure'},
        ]})
        self.assertEqual(value.failed_step_log, 'stderr:\nactual build failure')

    def test_multiple_unrecovered_scenarios_keep_suite_identity(self):
        value = state()
        value.execution_result.chaos_observation = {
            'type': 'scenario_suite', 'recovered': False,
            'scenarios': [{'chaos_observation': {'type': fault, 'recovered': False}}
                          for fault in ('dependency_database_outage', 'container_kill')],
        }
        packet = build_refiner_evidence(value)
        self.assertEqual(packet['chaos']['fault'], 'scenario_suite')
        self.assertEqual(len(packet['chaos']['scenarios']), 2)

    def test_suite_evidence_preserves_actual_dependency_failure(self):
        value = state()
        child = value.execution_result.chaos_observation.copy()
        value.execution_result.chaos_observation = {
            'type': 'scenario_suite', 'recovered': False,
            'scenarios': [{'scenario': 'dependency_database_outage', 'chaos_observation': child}],
        }
        for packet in (build_refiner_evidence(value), build_llm_evidence_packet(value)):
            self.assertEqual(packet['chaos']['fault'], 'dependency_database_outage')
            self.assertEqual(packet['chaos']['scenarios'], [{
                'fault': 'dependency_database_outage', 'recovered': False,
                'recovery_seconds': 2,
            }])

    def test_dependency_recovery_code_belongs_to_backend(self):
        value = state()
        with patch.object(source_context, 'collect', return_value={'app.py': 'old\n'}) as collect:
            workflow.attach_source_files(value)
        self.assertEqual(collect.call_args.args[0], 'https://github.com/o/backend.git')
        self.assertEqual(collect.call_args.kwargs['commit_sha'], BACKEND_SHA)
        self.assertEqual(value.source_files, {'app.py': 'old\n'})
        self.assertNotIn('.codereferee/validation.yaml', value.source_files)
        self.assertEqual(build_refiner_evidence(value)['source_repository']['name'], 'backend')
        self.assertEqual(build_llm_evidence_packet(value)['source_repository']['name'], 'backend')

    def test_replica_configuration_stays_in_primary_repository(self):
        value = state()
        value.execution_result.chaos_observation['type'] = 'container_kill'
        with patch.object(source_context, 'collect', return_value={}) as collect:
            workflow.attach_source_files(value)
        self.assertEqual(collect.call_args.args[0], value.repository_url)
        self.assertEqual(collect.call_args.kwargs['commit_sha'], MAIN_SHA)

    def test_explicit_failed_repository_owns_code(self):
        value = result()
        value.sandbox_report['failed_repository'] = 'backend'
        self.assertEqual(target_repository(value), 'backend')

    def test_ambiguous_dependency_owner_withholds_source(self):
        value = state()
        value.execution_result.source['fault_target'] = {'deployment': 'unknown'}
        with patch.object(source_context, 'collect') as collect:
            workflow.attach_source_files(value)
        collect.assert_not_called()
        self.assertFalse(value.source_files)

    def test_invalid_snapshot_does_not_redirect_source(self):
        value = state()
        value.execution_result.source['repositories']['backend']['url'] = 'https://credential@internal/x'
        with patch.object(source_context, 'collect') as collect:
            workflow.attach_source_files(value)
        collect.assert_not_called()

    def test_regeneration_applies_only_selected_repository_patch(self):
        value = state()
        with patch.object(source_context, 'collect', return_value={'app.py': 'new\n'}) as collect:
            workflow.attach_source_files(value, applied_patch='main diff',
                repository_patches={'backend': {'commitSha': BACKEND_SHA, 'patchDiff': DIFF}})
        self.assertEqual(collect.call_args.kwargs['applied_patch'], DIFF)

    def test_refiner_edits_emit_extra_repository_metadata(self):
        value = state()
        value.source_files = {'app.py': 'old\n'}
        value.metrics['source_repository'] = {'name': 'backend', 'url': 'https://github.com/o/backend.git', 'commitSha': BACKEND_SHA}
        response = {'summary': 'Repair connection recovery', 'edits': [{'path': 'app.py', 'find': ['old'], 'replace': ['new']}],
                    'patch_guidance': ['Reconnect'], 'verification_steps': ['Re-run'], 'risk': 'low'}
        with patch.object(nodes.llm, 'enabled', True), patch.object(nodes.llm, 'invoke_text', return_value=json.dumps(response)):
            nodes.refiner_node(value)
        self.assertEqual(value.refiner_report['patch_repository'], 'backend')
        self.assertEqual(value.refiner_report['patch_base_commit_sha'], BACKEND_SHA)
        self.assertIn('+new', value.refiner_report['patch_diff'])

    def test_anchor_retry_keeps_selected_repository_url(self):
        value = state()
        value.source_files = {'app.py': 'old\n'}
        url = 'https://github.com/o/backend.git'
        value.metrics['source_repository'] = {'name': 'backend', 'url': url, 'commitSha': BACKEND_SHA}
        def response(anchor):
            return json.dumps({'summary': 'Repair recovery',
                'edits': [{'path': 'app.py', 'find': [anchor], 'replace': ['new']}],
                'patch_guidance': ['Reconnect'], 'verification_steps': ['Re-run'], 'risk': 'low'})
        with patch.object(nodes.llm, 'enabled', True), patch.object(nodes.llm, 'invoke_text',
                side_effect=[response('absent'), response('old')]) as invoke:
            nodes.refiner_node(value)
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(invoke.call_args_list[1].args[2]['repository_url'], url)
        self.assertIn('+new', value.refiner_report['patch_diff'])


class RepositoryPatchGraphTests(unittest.TestCase):
    def prepare(self, value, applied=None, repositories=None):
        graph = workflow._refine_start_step({'agent': value})
        graph.update(agent=value, applied=applied, applied_repository_patches=repositories or {})
        graph.update(workflow._refine_prepare_step(graph))
        return graph

    def report(self, value, repository='backend', sha=BACKEND_SHA):
        value.refiner_report = {'patch_diff': DIFF, 'patch_repository': repository, 'patch_base_commit_sha': sha}

    def test_extra_patch_never_goes_to_legacy_primary_argument(self):
        value = state(); self.report(value)
        graph = self.prepare(value)
        with patch.object(workflow.sandbox_runner, 'run_repository', return_value=result(True)) as runner:
            workflow._refine_rerun_step(graph, workflow._graph_config(lambda *args, **kwargs: None))
        self.assertIsNone(runner.call_args.kwargs['patch_diff'])
        self.assertEqual(runner.call_args.kwargs['repository_patches']['backend']['patchDiff'], DIFF)
        self.assertEqual(runner.call_args.kwargs['repository_commits'], {'backend': BACKEND_SHA})
        self.assertEqual(runner.call_args.kwargs['commit_sha'], MAIN_SHA)

    def test_wrong_patch_base_stops_before_sandbox(self):
        value = state(); self.report(value, sha='c' * 40)
        self.assertTrue(self.prepare(value)['refine_stop'])

    def test_wrong_primary_patch_base_also_stops_before_sandbox(self):
        value = state(); self.report(value, repository='main', sha='c' * 40)
        self.assertTrue(self.prepare(value)['refine_stop'])

    def test_rerun_heartbeat_preserves_current_round(self):
        value = state(); self.report(value)
        graph = self.prepare(value)
        events = []
        heartbeat = workflow._sandbox_heartbeat

        def short_heartbeat(emit, step):
            return heartbeat(emit, step, interval=.02)

        def execute(*args, **kwargs):
            time.sleep(.08)
            return result(True)

        with patch.object(workflow, '_sandbox_heartbeat', side_effect=short_heartbeat), \
                patch.object(workflow.sandbox_runner, 'run_repository', side_effect=execute):
            workflow._refine_rerun_step(graph, workflow._graph_config(
                lambda step, **kwargs: events.append((step, kwargs))))
        beats = [kwargs for _, kwargs in events if kwargs.get('detail')]
        self.assertTrue(beats)
        self.assertTrue(all(item['round_'] == 1 and item['max_rounds'] == graph['max_rounds'] for item in beats))

    def test_target_switch_keeps_other_repository_patch(self):
        value = state(); self.report(value, repository='main', sha=MAIN_SHA)
        old = {'backend': {'commitSha': BACKEND_SHA, 'patchDiff': DIFF}}
        graph = self.prepare(value, repositories=old)
        self.assertEqual(graph['pending_main_patch'], DIFF)
        self.assertEqual(graph['pending_repository_patches'], old)

    def test_size_limit_counts_all_repositories(self):
        value = state(); self.report(value)
        settings = get_settings().model_copy(update={'max_patch_diff_bytes': len(DIFF.encode()) + 1})
        with patch.object(workflow, 'get_settings', return_value=settings):
            self.assertTrue(self.prepare(value, applied=DIFF)['refine_stop'])

    def test_two_round_extra_patch_accumulates_within_same_repository(self):
        value = state(); self.report(value)
        old = {'backend': {'commitSha': BACKEND_SHA, 'patchDiff': DIFF}}
        graph = self.prepare(value, repositories=old)
        self.assertEqual(graph['pending_repository_patches']['backend']['patchDiff'], DIFF + DIFF)
        self.assertIsNone(graph['pending_main_patch'])

    def test_switch_between_additional_repositories_keeps_both_patches(self):
        value = state()
        value.execution_result.source['repositories']['worker'] = {
            'url': 'https://github.com/o/worker.git', 'commitSha': 'c' * 40}
        self.report(value, repository='worker', sha='c' * 40)
        old = {'backend': {'commitSha': BACKEND_SHA, 'patchDiff': DIFF}}
        graph = self.prepare(value, repositories=old)
        self.assertEqual(set(graph['pending_repository_patches']), {'backend', 'worker'})
        self.assertEqual(graph['pending_repository_patches']['backend'], old['backend'])
        self.assertIsNone(graph['pending_main_patch'])

    def test_submitted_failure_is_preserved_after_successful_extra_patch(self):
        value = state(); self.report(value)
        submitted = value.execution_result
        with patch.object(workflow.sandbox_runner, 'run_repository', return_value=result(True)):
            returned = workflow._run_refinement_rounds(value, lambda *args, **kwargs: None)
        self.assertEqual(returned.status, JobStatus.failed)
        self.assertIs(returned.execution_result, submitted)
        self.assertTrue(returned.metrics['patch_verified'])
        self.assertEqual(returned.refine_rounds[0]['patch_repository'], 'backend')

    def test_two_repositories_are_repaired_sequentially_with_one_shared_limit(self):
        value = state(); self.report(value)
        submitted = value.execution_result
        failed = result(False)
        failed.chaos_observation['type'] = 'container_kill'
        calls = []

        def execute(*args, **kwargs):
            calls.append(kwargs)
            return failed if len(calls) == 1 else result(True)

        def regenerate(current):
            self.report(current, repository='main', sha=MAIN_SHA)
            return current

        with patch.object(workflow.sandbox_runner, 'run_repository', side_effect=execute), \
                patch.object(workflow, 'attach_source_files'), \
                patch.object(workflow, 'critic_node', side_effect=lambda current: current), \
                patch.object(workflow, 'refiner_node', side_effect=regenerate):
            returned = workflow._run_refinement_rounds(value, lambda *args, **kwargs: None)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0]['patch_diff'])
        self.assertEqual(calls[1]['patch_diff'], DIFF)
        self.assertEqual(calls[0]['repository_patches'], calls[1]['repository_patches'])
        self.assertEqual([r.get('patch_repository', 'main') for r in returned.refine_rounds], ['backend', 'main'])
        self.assertTrue(returned.metrics['patch_verified'])
        self.assertEqual(returned.status, JobStatus.failed)
        self.assertIs(returned.execution_result, submitted)

    def test_target_switch_does_not_reset_total_round_budget(self):
        value = state(); self.report(value)
        settings = get_settings().model_copy(update={'max_self_healing_retries': 3})
        rounds = []

        def execute(*args, **kwargs):
            rounds.append(kwargs)
            return result(False)

        def regenerate(current):
            self.report(current, repository='main' if len(rounds) % 2 else 'backend',
                        sha=MAIN_SHA if len(rounds) % 2 else BACKEND_SHA)
            return current

        with patch.object(workflow, 'get_settings', return_value=settings), \
                patch.object(workflow.sandbox_runner, 'run_repository', side_effect=execute), \
                patch.object(workflow, 'attach_source_files'), \
                patch.object(workflow, 'critic_node', side_effect=lambda current: current), \
                patch.object(workflow, 'refiner_node', side_effect=regenerate):
            returned = workflow._run_refinement_rounds(value, lambda *args, **kwargs: None)
        self.assertEqual(len(rounds), 3)
        self.assertEqual(len(returned.refine_rounds), 3)
        self.assertFalse(returned.metrics['patch_verified'])


class PinnedSourceCollectTests(unittest.TestCase):
    def test_collect_applies_utf8_patch_to_lf_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '--quiet')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('config', 'core.autocrlf', 'false')
            (repo / 'app.py').write_bytes('value = "원본"\n'.encode('utf-8'))
            git('add', 'app.py')
            git('commit', '--quiet', '-m', 'initial')
            diff = '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = "원본"\n+value = "수정"\n'
            files = source_context.collect(str(repo), ['app.py'], commit_sha=git('rev-parse', 'HEAD'),
                                           applied_patch=diff)
            self.assertEqual(files, {'app.py': 'value = "수정"\n'})

    def test_collect_reads_original_commit_after_branch_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'; repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '--quiet'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'app.py').write_text('original\n'); git('add', 'app.py'); git('commit', '--quiet', '-m', 'initial')
            commit = git('rev-parse', 'HEAD')
            (repo / 'app.py').write_text('moved\n'); git('commit', '--quiet', '-am', 'move')
            self.assertEqual(source_context.collect(str(repo), ['app.py'], commit_sha=commit), {'app.py': 'original\n'})
