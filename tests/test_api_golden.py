# SPDX-License-Identifier: MPL-2.0
"""The API reference exercises real CLI branches and cannot fall back to I/O."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tests/golden/capture_api.py'
INPUTS = SCRIPT.parent / 'api/inputs'
BASELINE = SCRIPT.parent / 'api/baseline'
spec = importlib.util.spec_from_file_location('api_golden_harness', SCRIPT)
REPLAY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(REPLAY)


def snapshot(name):
    return REPLAY.read_json(BASELINE / (name + '-json.json'))


def envelope(name):
    result = snapshot(name)
    return json.loads(result['stderr'] if result['exitCode'] == 1 else result['stdout'])


class ApiGoldenBoundaryTest(unittest.TestCase):
    def host(self, events=(), responses=None):
        # A base without any process methods makes a fallback impossible even
        # if a test accidentally stops using one of the explicit refusals.
        return REPLAY.replay_host(types.SimpleNamespace(Host=object), list(events), responses or {},
                                  pathlib.Path('/fixture'), ROOT)

    def test_unknown_reordered_and_unused_calls_fail(self):
        first = {'call': {'kind': 'which', 'name': 'gh'}, 'result': '/recorded/gh'}
        second = {'call': {'kind': 'read', 'path': 'repos/example/product'}, 'response': 'reply'}
        host = self.host([first, second], {'reply': {'state': 'ok', 'body': {}}})
        with self.assertRaisesRegex(AssertionError, 'unrecorded host call'):
            host.read('repos/example/product')
        self.assertEqual(host.which('gh'), '/recorded/gh')
        with self.assertRaisesRegex(AssertionError, 'not consumed'):
            host.finish()
        self.assertEqual(host.read('repos/example/product'), ('ok', {}))
        host.finish()
        with self.assertRaisesRegex(AssertionError, 'unrecorded host call'):
            host.which('gh')

    def test_unknown_processes_and_environments_fail(self):
        host = self.host()
        for command in (['git', 'push', 'origin'], ['gh', 'api', 'repos/example/product'], ['sh', '-c', 'true']):
            with self.subTest(command=command), self.assertRaisesRegex(AssertionError, 'unrecorded host call'):
                host.run(command)
        with self.assertRaisesRegex(AssertionError, 'environment'):
            host.run(['git', 'rev-parse', 'HEAD'], env={})

    def test_writers_streams_and_exec_are_always_refused(self):
        host = self.host()
        for method in (host.write, host.stream, host.exec):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(AssertionError, 'forbids'):
                method('anything')

    def test_one_read_cannot_mutate_the_next_response(self):
        event = {'call': {'kind': 'read', 'path': 'repos/example/product'}, 'response': 'reply'}
        host = self.host([event, event], {'reply': {'state': 'ok', 'body': {'contexts': ['check']}}})
        host.read('repos/example/product')[1]['contexts'].clear()
        self.assertEqual(host.read('repos/example/product'), ('ok', {'contexts': ['check']}))
        host.finish()

    def test_only_local_adoption_may_apply(self):
        REPLAY.validate_argv(['adopt', '/fixture', '--apply'])
        for argv in (['protect', '--apply'], ['migrate', '--push'], ['migrate', '--push=true'],
                     ['migrate', '--apply'], ['cut'], ['tap'], ['vendor'], []):
            with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                REPLAY.validate_argv(argv)

    def test_a_version_bump_and_a_changelog_entry_change_nothing_in_the_replay(self):
        """The defect the first cut after the recording revealed: VERSION 0.59.0
        made every recorded release.yml a drift, and the release pull request's
        self-test red with 27 unconsumed host calls. VERSION and CHANGELOG.md are
        inputs of the reference, restored by the replay, so a candidate at any
        version replays the same."""
        with tempfile.TemporaryDirectory() as directory:
            bumped = pathlib.Path(directory) / 'bumped'
            for name in ('bin', 'share', 'docs', 'tests', '.github/workflows'):
                shutil.copytree(ROOT / name, bumped / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            for name in ('WITHDRAWN',):
                shutil.copy2(ROOT / name, bumped / name)
            (bumped / 'VERSION').write_text('9.9.9\n')
            (bumped / 'CHANGELOG.md').write_text('# Changelog\n\n## 9.9.9 — 2099-01-01\n\n- **Impact.** [asks: nothing] x\n\n'
                                                 + (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'))
            output = pathlib.Path(directory) / 'replay'
            completed = self.replay(output, '--source', str(bumped), '--case', 'preflight-ruleset-text',
                                    '--case', 'current-private-open-adopt-json')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            for case in ('preflight-ruleset-text', 'current-private-open-adopt-json'):
                self.assertEqual(REPLAY.read_json(output / f'{case}.json'),
                                 REPLAY.read_json(ROOT / 'tests/golden/api/baseline' / f'{case}.json'), case)

    def test_changed_or_unhashed_inputs_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = pathlib.Path(directory) / 'inputs'
            shutil.copytree(INPUTS, inputs)
            REPLAY.verify_inputs(inputs)
            response = inputs / 'responses.json'
            response.write_bytes(response.read_bytes() + b'\n')
            with self.assertRaisesRegex(RuntimeError, 'digest differs'):
                REPLAY.verify_inputs(inputs)
            manifest = REPLAY.read_json(inputs / 'manifest.json')
            del manifest['sha256']['responses.json']
            REPLAY.write_json(inputs / 'manifest.json', manifest)
            with self.assertRaisesRegex(RuntimeError, 'every input'):
                REPLAY.verify_inputs(inputs)

    def replay(self, output, *options):
        return subprocess.run([sys.executable, str(SCRIPT), str(output), *options],
                              env={**os.environ, '_MAELYS_RELEASE_SELF_TEST': 'invalid-in-parent',
                                   '_MAELYS_RELEASE_TEST_GH': '/nonexistent/gh'},
                              capture_output=True, text=True, timeout=90)

    def test_real_cli_replays_with_poisoned_parent_environment(self):
        cases = ['protect-classic-json', 'preflight-ruleset-text', 'current-private-open-adopt-json',
                 'current-private-unreadable-adopt-json', 'adoption-guard-ruleset-refused-json', 'migration-json']
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / 'replay'
            options = [arg for case in cases for arg in ('--case', case)]
            completed = self.replay(output, *options)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            for case in cases:
                with self.subTest(case=case):
                    self.assertEqual((output / (case + '.json')).read_bytes(),
                                     (BASELINE / (case + '.json')).read_bytes())
            refused = self.replay(output, *options)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn('refusing to overwrite', refused.stderr)

    def test_changed_host_transcript_fails_without_publishing_a_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs, output = pathlib.Path(directory) / 'inputs', pathlib.Path(directory) / 'replay'
            shutil.copytree(INPUTS, inputs)
            path = inputs / 'cases/protect-classic-json.json'
            case = REPLAY.read_json(path)
            next(event for event in case['events'] if event['call']['kind'] == 'read')['call']['path'] += '/unexpected'
            REPLAY.write_json(path, case)
            manifest = REPLAY.read_json(inputs / 'manifest.json')
            manifest['sha256'][path.relative_to(inputs).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            REPLAY.write_json(inputs / 'manifest.json', manifest)
            completed = self.replay(output, '--inputs', str(inputs), '--case', 'protect-classic-json')
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn('unrecorded host call', completed.stderr)
            self.assertFalse(output.exists())


class ApiGoldenCoverageTest(unittest.TestCase):
    """Check the reference's reach, not only its agreement with the candidate."""
    def test_recorded_and_derived_responses_have_distinct_provenance(self):
        manifest = REPLAY.verify_inputs(INPUTS)
        responses = REPLAY.read_json(INPUTS / 'responses.json')
        kinds = set()
        for key, value in responses.items():
            provenance = value['provenance']
            kinds.add(provenance['kind'])
            if provenance['kind'] == 'live':
                self.assertEqual(provenance['endpoint'], key)
                self.assertEqual(provenance['method'], 'GET')
                self.assertTrue(provenance['recordedAt'])
                self.assertIn(value['state'], ('ok', 'absent'))
            else:
                self.assertEqual(provenance['kind'], 'derived')
                self.assertEqual(responses[provenance['from']]['provenance']['kind'], 'live')
                self.assertTrue(provenance['changes'])
        self.assertEqual(kinds, {'live', 'derived'})
        self.assertEqual({case['format'] for case in manifest['cases']}, {'text', 'json'})
        for case in manifest['cases']:
            self.assertIn(case['id'].rsplit('-', 1)[0] + '-text', {c['id'] for c in manifest['cases']})
            self.assertIn(case['id'].rsplit('-', 1)[0] + '-json', {c['id'] for c in manifest['cases']})

    def test_protect_covers_settings_rulesets_absence_and_unknown(self):
        classic = envelope('protect-classic')['data']
        self.assertTrue(classic['protected'])
        self.assertTrue(classic['keeps'])
        self.assertTrue(classic['observed'])
        # The aliases left in 0.60.0: nothing is replaced pair by pair any
        # more, and what the branch required before the command is kept in
        # the output for a hand.
        self.assertEqual(classic['replaced'], [])
        self.assertTrue(classic['before']['required'])
        self.assertIsInstance(classic['before']['classic'], dict)
        self.assertIn('enforce_admins', classic['before']['classic'])
        ruleset = envelope('protect-ruleset')['data']
        self.assertEqual(ruleset['protectedBy'], ['a ruleset'])
        self.assertTrue(ruleset['ruleset'])
        self.assertTrue(envelope('protect-open')['data']['creates'])
        unknown = envelope('protect-unreadable')['data']
        self.assertTrue(unknown['unread'])
        self.assertNotIn('creates', unknown)
        self.assertTrue(envelope('protect-partial-observations')['data']['unread'])
        narrow = envelope('protect-narrow-plan')['data']
        self.assertTrue(narrow['narrowing'])
        self.assertTrue(narrow['dropped'])

    def test_preflight_reaches_api_checks_and_failure(self):
        for name, ready in (('classic', True), ('ruleset', True), ('private', True), ('open', True),
                            ('missing-environment', False), ('widening-policy', False),
                            ('never-published', True), ('unreadable-environment', True)):
            data = envelope('preflight-' + name)['data']
            self.assertTrue(data['valid'], name)  # Local checks must not short-circuit API checks.
            self.assertTrue(data['preflight'], name)
            self.assertIs(data['ready'], ready, name)
        self.assertTrue(any('a ruleset' in item['message'] for item in envelope('preflight-ruleset')['data']['preflight']))
        self.assertTrue(any(item['status'] == 'fail' for item in envelope('preflight-missing-environment')['data']['preflight']))

    def test_preflight_reads_the_whole_policy_list_and_reports_the_record(self):
        # A policy list is a union: the tag rule found beside a branch policy
        # used to answer "limits deployments to tags v*". And ready says the
        # tag would not be refused, never that a package comes out: the
        # record is what tells the two apart, so it is recorded at both ends.
        widening = [item for item in envelope('preflight-widening-policy')['data']['preflight']
                    if item['status'] == 'fail']
        self.assertTrue(any('also admits the branch main' in item['message'] for item in widening), widening)
        self.assertTrue(any('-X DELETE' in item['message'] for item in widening), widening)
        for name, said in (('classic', 'the last publication of'), ('never-published', 'has published yet')):
            record = [item for item in envelope('preflight-' + name)['data']['preflight'] if said in item['message']]
            self.assertEqual([item['status'] for item in record], ['note'], name)
        # The boundary is stated where ready is, not only in the conventions.
        never = envelope('preflight-never-published')['data']
        self.assertIs(never['ready'], True)
        self.assertTrue(any('rehearse is what builds' in item['message'] for item in never['preflight']))
        # A reading GitHub refused is a note naming it, not an environment
        # that does not exist -- and it does not close the gate, which
        # GitHub applies when a job asks for the environment.
        refused = [item for item in envelope('preflight-unreadable-environment')['data']['preflight']
                   if 'environment release' in item['message']]
        self.assertEqual([item['status'] for item in refused], ['note'])
        self.assertIn('could not be read (unreadable)', refused[0]['message'])
        missing = [item for item in envelope('preflight-missing-environment')['data']['preflight']
                   if 'environment release' in item['message']]
        self.assertEqual([item['status'] for item in missing], ['fail'])

    def test_public_and_classic_selectors_cover_all_current_verdicts(self):
        for profile, current in (('live', False), ('private', False), ('private-open', True), ('private-unreadable', None)):
            with self.subTest(profile=profile):
                data = envelope('current-' + profile + '-adopt')['data']
                self.assertIs(data['current'], current)
                self.assertIs(data['impact'][0]['asksThis'], None if current is None else not current)
                check = envelope('current-' + profile + '-check')['data']
                self.assertIs(check['release']['valid'], current is True)

    def test_adoption_guard_refuses_before_files_change_in_both_protection_models(self):
        for protection in ('classic', 'ruleset'):
            refused = 'adoption-guard-' + protection + '-refused'
            self.assertEqual(snapshot(refused)['exitCode'], 1)
            self.assertEqual(envelope(refused)['error']['code'], 'PRECONDITION_FAILED')
            self.assertEqual(snapshot(refused)['changedFiles'], {})
            allowed = 'adoption-guard-' + protection + '-allowed'
            self.assertEqual(snapshot(allowed)['exitCode'], 0)
            self.assertTrue(snapshot(allowed)['changedFiles'])
        # Existing behavior, recorded explicitly: an unreadable protection is
        # not a detected lock. A behavior fix must explain this golden diff.
        self.assertEqual(snapshot('adoption-guard-unreadable')['exitCode'], 0)
        self.assertTrue(snapshot('adoption-guard-unreadable')['changedFiles'])

    def test_migration_plan_covers_moving_staying_and_invalid_records(self):
        data = envelope('migration')['data']
        self.assertEqual(data['documents'], 1)
        self.assertIn('README.md', data['moving'][0]['referencedBy'])
        reasons = {item['path']: item['reason'] for item in data['staying']}
        self.assertEqual(reasons['docs/generated.md'], 'generated: its head says so')
        self.assertEqual(reasons['docs/public.md'], 'LICENSING.md engages it publicly')
        self.assertEqual(reasons['docs/data.json'], 'data, not prose')
        self.assertEqual(snapshot('migration')['changedFiles'], {})
        for name in ('migration-foreign', 'migration-wrong-destination'):
            self.assertEqual(envelope(name)['error']['code'], 'VALIDATION_FAILED')
            self.assertEqual(snapshot(name)['changedFiles'], {})


class ApiGoldenSeededNameTest(unittest.TestCase):
    """The seeded line naming the documentation repository, recorded at each stage.

    0.60.0 noted it; 0.61.0 refuses it, announced. This case records what
    the refusal does: check exits 2, and preflight stops on the violation
    before its GitHub reads.
    """

    NOTE = 'LICENSING.md names the documentation repository'

    def test_check_refuses_the_line(self):
        data = envelope('seeded-name-check')['data']
        said = [item for item in data['checks'] if item['message'].startswith(self.NOTE)]
        self.assertEqual([item['status'] for item in said], ['missing'])
        self.assertFalse(data['conventions']['valid'])
        self.assertEqual(snapshot('seeded-name-check')['exitCode'], 2)

    def test_preflight_stops_on_the_violation_before_github(self):
        # The refusal is a conventions violation, and preflight stops on one
        # before its GitHub reads: recorded, so that the reference says what
        # 0.61.0 does to a product that kept the line -- and the other
        # preflight cases, on fixtures with the 0.59.2 seed, still reach them.
        data = envelope('seeded-name-preflight')['data']
        self.assertTrue(any(item['message'].startswith(self.NOTE) for item in data['checks']))
        self.assertFalse(data['valid'])
        self.assertEqual(data['preflight'], [])
