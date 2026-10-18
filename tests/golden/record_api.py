#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Explicit bootstrap/refresh of API inputs from a clean, named source commit.

This tool alone can read GitHub. It cannot issue a GitHub write or run a
product publication. Normal CI uses capture_api.py, which cannot call gh/git.
The responses cache preserves every first live reading across interrupted
recordings; refreshing live data requires a different, explicit cache path.

--reference INPUTS records a behavior fix that changes the host calls: the
candidate's code runs under the socle/VERSION and socle/CHANGELOG.md frozen
in INPUTS, as capture.py --candidate does for the other reference, so the
recording holds the fix and not the versions cut since the last one. INPUTS
is verified like any inputs a replay reads, digest by digest, before a line
of it is used. --offline refuses every read the cache does not answer:
without it a cache miss is read live from GitHub and recorded.

The manifest says both things a reader needs and cannot infer: sourceCommit
and sourceVersion are the commit recorded and the VERSION that commit
carries; frozenVersion is the VERSION the recording ran under, which is the
one a replay restores.

No answer is stored with `security_and_analysis`: GitHub gives that member
to a reader who administers the repository, it says whether secret scanning
and push protection are on, and the state of a real repository's security
settings is not this reference's to publish. It is dropped from every
answer, read live or taken from the cache, at whatever depth it stands,
before the answer is given to the recorded program -- so what is recorded
is what the program was told, and a recorded `preflight` says nothing of
those settings. The note `preflight` makes of them is held by tests on
constructed answers, in tests/test_privacy.py.

A 404 is stored with the words GitHub answered it in, as `message` beside
its state: `protect` takes a 404 for an absence only where it says `Branch
not protected`, and an answer stored without its words is given back as an
absence that says nothing. The open branch the reference derives is that
state, so it carries GitHub's words for it.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import capture_api as replay


REPOSITORIES = ('maelys-dev/maelys-system', 'maelys-dev/agent-cli-spec')
DATE = '2026-09-16T00:00:00+0000'
# What git would compose on the recording machine -- its account's name, its
# login, its own name -- is never an input: it is exactly what the reading
# that asks for it exists to keep out of a public file. The question is one
# argv, spelled here as the program spells it; the recorder answers it
# itself, with a machine that does not exist, and never runs it, so that what
# is recorded is what the recorded program was told. Any other use of env(1)
# is refused like any process outside the list below.
COMPOSED = ['env', '-u', 'EMAIL', '-u', 'GIT_AUTHOR_NAME', '-u', 'GIT_AUTHOR_EMAIL', '-u', 'GIT_COMMITTER_NAME',
            '-u', 'GIT_COMMITTER_EMAIL', '-u', 'GIT_DIR', '-u', 'GIT_WORK_TREE', '-u', 'GIT_CONFIG_COUNT',
            '-u', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_GLOBAL=/dev/null', 'GIT_CONFIG_SYSTEM=/dev/null',
            'GIT_CONFIG_NOSYSTEM=1', 'git', '-C', '/', 'var', 'GIT_COMMITTER_IDENT']
MACHINE = {'env': '/usr/bin/env',
           'stdout': 'Golden Machine <golden@golden-machine.invalid> 1757980800 +0000\n'}
# What an answer says of a real repository and the reference does not
# publish: see the header. One member today; a name added here is dropped
# from every answer at the next recording, and from nothing already stored.
WITHHELD = ('security_and_analysis',)
# What GitHub answers, with its 404, for a branch that carries no classic
# protection: the words of the open branch this recorder derives. Spelled
# here as the program spells them, and held against it by a test.
NOT_PROTECTED = 'Branch not protected'


def withheld(value):
    """VALUE as GitHub answered it, without the members the reference does not publish."""
    if isinstance(value, dict):
        return {key: withheld(item) for key, item in value.items() if key not in WITHHELD}
    if isinstance(value, list):
        return [withheld(item) for item in value]
    return value


def git(project, *args):
    return subprocess.run(['git', '-C', str(project.resolve()), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull,
                          'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                          'GIT_AUTHOR_DATE': DATE, 'GIT_COMMITTER_DATE': DATE}).stdout.strip()


# The search for the copies of a pin, as the program spells it: `git grep`
# with six settings of the user's configuration set back on the command
# itself. A `-c` before the subcommand can make git do anything -- an alias,
# a pager, a hooks path -- so no `git -c` is allowed in general: these six
# settings, in this order, and then `grep`. A test holds the list against
# the program's, so that a setting added there is allowed here or the
# recording of a product with a pin stops, naming the command.
PIN_SEARCH = ['git', '-c', 'color.grep=false', '-c', 'grep.column=false', '-c', 'grep.fullName=false',
              '-c', 'grep.fallbackToNoIndex=false', '-c', 'submodule.recurse=false', '-c', 'core.quotePath=true',
              'grep']


def safe_git(command):
    if command[:len(PIN_SEARCH)] == PIN_SEARCH:
        return True
    prefixes = (['git', 'remote', 'get-url', 'origin'], ['git', 'config', '--get'],
                ['git', 'tag', '--list'], ['git', 'rev-parse'], ['git', 'grep'], ['git', 'ls-files'],
                ['git', 'describe', '--tags', '--abbrev=0'], ['git', 'cat-file', '-t'],
                ['git', 'cat-file', '-p'], ['git', 'merge-base', '--is-ancestor'],
                # Reads of the identity and of what a branch has ahead of its upstream.
                ['git', 'config', '-z', '--get-regexp'], ['git', 'symbolic-ref', '-q', '--short', 'HEAD'],
                ['git', 'log', '--boundary'], ['git', 'for-each-ref'])
    return any(command[:len(prefix)] == prefix for prefix in prefixes)


def fixture(module, parent, repository, commit, kind):
    project = parent / repository.split('/')[-1]
    project.mkdir()
    files = {'VERSION': '1.2.3\n', 'CHANGELOG.md': '# Changelog\n\n## 1.2.3 — 2026-09-16\n\n- Fixture.\n',
             'maelys-release.conf': '[gate]\nnone\n', 'scripts/package-release.sh': '#!/bin/sh\nexit 0\n'}
    for name, value in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
    (project / 'scripts/package-release.sh').chmod(0o755)
    # Build managed files using the named, clean implementation, before origin
    # exists. This changes only a temporary product, never a fleet checkout.
    original = replay.socle_host(module).HOST
    class Bootstrap(replay.socle_host(module).Host):
        def read(self, path):
            raise AssertionError(f'fixture setup attempted GitHub: {path}')
        def which(self, name):
            return None
    replay.socle_host(module).HOST = Bootstrap()
    try:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.APP.main(['adopt', str(project), '--mechanism', 'maelys-release', '--apply',
                                    '--socle-sha', commit, '--socle-tag', 'v' + module.socle_version(),
                                    '--format', 'json'])
        if code:
            raise RuntimeError(err.getvalue())
    finally:
        replay.socle_host(module).HOST = original
    if kind == 'seeded-name':
        # The line the seed of 0.58.0 to 0.59.1 wrote for every product: a
        # note since 0.60.0, a refusal from 0.61.0 -- this case is where the
        # reference records each, and what the refusal does to preflight.
        licensing = project / 'LICENSING.md'
        licensing.write_text(licensing.read_text(encoding='utf-8')
                             + '\nProse that is not engaged migrates to `maelys-docs/' + project.name + '/`.\n',
                             encoding='utf-8')
    if kind == 'old-pin':
        for path in (project / '.github/workflows').glob('*.yml'):
            text = path.read_text(encoding='utf-8')
            path.write_text(text.replace(commit + ' # v' + module.socle_version(), '0' * 40 + ' # v0.57.2'), encoding='utf-8')
    if kind.startswith('migration'):
        (project / 'docs').mkdir(exist_ok=True)
        for name, text in {'guide.md': '# Internal guide\n\nA document to move.\n',
                           'public.md': '# Public promise\n',
                           'generated.md': '<!-- Generated by fixture. Do not edit. -->\n',
                           'unlisted.md': '# Unlisted prose\n', 'data.json': '{}\n'}.items():
            (project / 'docs' / name).write_text(text, encoding='utf-8')
        (project / 'README.md').write_text('[guide](docs/guide.md)\n', encoding='utf-8')
        licensing = project / 'LICENSING.md'
        licensing.write_text(licensing.read_text(encoding='utf-8') + '\n- [Public promise](docs/public.md).\n', encoding='utf-8')
        record = {'repository': project.name, 'path': 'docs/guide.md',
                  'destination': f'golden-docs/{project.name}/guide.md'}
        if kind == 'migration-foreign':
            record['repository'] = 'another-product'
        if kind == 'migration-wrong-destination':
            record['destination'] = 'elsewhere/guide.md'
        (project / 'documents.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')
    git(project, 'init', '-q', '-b', 'main')
    git(project, 'config', 'user.name', 'Golden fixture')
    git(project, 'config', 'user.email', 'golden@example.invalid')
    git(project, 'config', 'commit.gpgsign', 'false')
    git(project, 'add', '-A')
    git(project, 'commit', '-q', '-m', 'Frozen API fixture')
    git(project, 'remote', 'add', 'origin', 'https://github.com/' + repository + '.git')
    git(project, 'remote', 'set-url', '--push', 'origin', 'disabled://golden-read-only')
    git(project, 'config', 'tag.gpgsign', 'true')
    git(project, 'config', 'gpg.format', 'ssh')
    git(project, 'config', 'user.signingkey', 'golden-fixture-key-not-used')
    return project


def scenarios():
    cases = []
    def add(name, repository, profile, command, kind='current-pin', options=()):
        for fmt in ('json', 'text'):
            cases.append({'id': name + '-' + fmt, 'repository': repository, 'profile': profile,
                          'command': command, 'fixture': kind, 'options': list(options), 'format': fmt})
    system, ruleset = REPOSITORIES
    for name, repository, profile in (('classic', system, 'live'), ('ruleset', ruleset, 'live'),
                                      ('open', system, 'open'), ('unreadable', system, 'unreadable'),
                                      ('partial-observations', system, 'partial')):
        add('protect-' + name, repository, profile, 'protect')
    add('protect-narrow-plan', system, 'live', 'protect', options=('--without-legs',))
    for name, repository, profile in (('classic', system, 'live'), ('ruleset', ruleset, 'live'),
                                      ('private', system, 'private'), ('open', system, 'open'),
                                      ('missing-environment', system, 'missing-environment'),
                                      ('widening-policy', system, 'widening-policy'),
                                      ('never-published', system, 'never-published'),
                                      ('unreadable-environment', system, 'unreadable-environment')):
        add('preflight-' + name, repository, profile, 'preflight')
    for profile in ('live', 'private', 'private-open', 'private-unreadable'):
        for command in ('adopt', 'check'):
            add('current-' + profile + '-' + command, system, profile, command, kind='old-pin')
    for name, repository, profile in (('classic-allowed', system, 'live'), ('ruleset-allowed', ruleset, 'live'),
                                      ('classic-refused', system, 'stale'), ('ruleset-refused', ruleset, 'stale'),
                                      ('unreadable', system, 'unreadable')):
        add('adoption-guard-' + name, repository, profile, 'adopt', kind='old-pin', options=('--apply',))
    for kind in ('migration', 'migration-foreign', 'migration-wrong-destination'):
        add(kind, system, 'live', 'migrate', kind=kind)
    for command in ('check', 'preflight'):
        add('seeded-name-' + command, system, 'live', command, kind='seeded-name')
    return cases


class Responses:
    def __init__(self, original, cache, offline=False):
        self.original, self.cache, self.offline = original, cache, offline
        # Withheld on the way in, whichever way that is: a cache written by
        # an earlier recorder holds the member, and the file is left as it
        # is -- a cache nothing was added to is not rewritten.
        self.live = {endpoint: {**answer, 'body': withheld(answer.get('body'))}
                     for endpoint, answer in (replay.read_json(cache) if cache.exists() else {}).items()}
        self.used = {}

    def read(self, endpoint, profile):
        product_read = any(endpoint == 'repos/' + repo or endpoint.startswith('repos/' + repo + '/')
                           for repo in REPOSITORIES)
        tap_read = re.fullmatch(r'repos/maelys-dev/homebrew-tap/contents/Formula(?:/[A-Za-z0-9_.-]+\.rb)?', endpoint)
        if not product_read and not tap_read:
            raise AssertionError(f'API endpoint outside the recording scope: {endpoint}')
        if endpoint not in self.live:
            if self.offline:
                # Said before anything is asked: a recording that promised to
                # read nothing live must not find out afterwards that it did.
                raise RuntimeError(f'live read refused (--offline): {endpoint} is not in the responses cache')
            state, body = self.original.read(endpoint)
            if state not in ('ok', 'absent'):
                raise RuntimeError(f'live GET {endpoint}: {state}; do not record a transient failure as live truth')
            self.live[endpoint] = {'state': str(state), 'body': withheld(body), 'provenance': {'kind': 'live', 'method': 'GET',
                'endpoint': endpoint, 'recordedAt': datetime.datetime.now(datetime.timezone.utc).isoformat()}}
            # The words of a 404, which the state alone does not keep: one
            # status answers a branch without protection, a token that may
            # not read one and a branch that does not exist.
            if getattr(state, 'message', ''):
                self.live[endpoint]['message'] = state.message
            replay.write_json(self.cache, self.live)
        answer = copy.deepcopy(self.live[endpoint])
        self.used[endpoint] = copy.deepcopy(answer)
        changes = []
        if profile.startswith('private') and endpoint.count('/') == 2:
            answer['body']['visibility'], answer['body']['private'] = 'private', True
            changes.append('repository visibility becomes private')
        if profile in ('open', 'private-open'):
            if endpoint.endswith('/protection'):
                answer.update(state='absent', body=None, message=NOT_PROTECTED)
                changes.append('classic protection is absent')
                changes.append(f"its 404 says '{NOT_PROTECTED}', GitHub's words for that state")
            elif '/rules/branches/' in endpoint:
                answer.update(state='ok', body=[])
                changes.append('no branch rules apply')
        if profile in ('unreadable', 'private-unreadable') and (endpoint.endswith('/protection') or '/rules/branches/' in endpoint):
            answer.update(state='unreadable', body=None)
            changes.append('protection/rules reading is unreadable')
        if profile == 'partial' and '/check-runs?' in endpoint:
            answer.update(state='unreadable', body=None)
            changes.append('check-run reading is unreadable')
        if profile == 'missing-environment' and '/environments/release' in endpoint:
            answer.update(state='absent', body=None)
            changes.append('release environment is absent')
        # The two facts 0.61.0 added to preflight: a policy list is read
        # whole, and the publication record is reported beside the verdict.
        # Neither has an end on the fleet to record live -- every repository
        # carries the tag rule alone, and every one of them has published --
        # so both are derived from what does answer.
        if profile == 'widening-policy' and 'deployment-branch-policies' in endpoint:
            answer['body']['branch_policies'].append({'id': 90000001, 'name': 'main', 'type': 'branch'})
            answer['body']['total_count'] = len(answer['body']['branch_policies'])
            changes.append('a branch policy sits beside the tag rule v*')
        if profile == 'never-published' and '/releases?' in endpoint:
            answer.update(state='ok', body=[])
            changes.append('the repository has never published a release')
        # A reading GitHub refused, which the socle used to report as an
        # environment that does not exist.
        if profile == 'unreadable-environment' and endpoint.endswith('/environments/release'):
            answer.update(state='unreadable', body=None)
            changes.append('the release environment is unreadable')
        if profile == 'stale':
            if endpoint.endswith('/protection') and answer['state'] == 'ok':
                answer['body']['required_status_checks']['contexts'].append('check / check (retired-golden)')
                changes.append('classic protection additionally requires a retired socle leg')
            elif '/rules/branches/' in endpoint:
                for rule in answer['body'] or []:
                    if rule.get('type') == 'required_status_checks':
                        rule['parameters']['required_status_checks'].append({'context': 'check / check (retired-golden)'})
                        changes.append('ruleset additionally requires a retired socle leg')
                        break
        # The words of a 404 are said of a 404: an answer derived into
        # another state does not keep those of the reading it came from.
        if answer['state'] != 'absent':
            answer.pop('message', None)
        key = endpoint
        if changes:
            key = profile + ':' + endpoint
            answer['provenance'] = {'kind': 'derived', 'from': endpoint, 'changes': changes}
        self.used[key] = answer
        return key, answer


def recording_host(module, project, source, responses, profile):
    original = replay.socle_host(module).Host()
    class RecordingHost(replay.socle_host(module).Host):
        def __init__(self):
            self.events = []
        def which(self, name):
            if name == 'env':
                self.events.append({'call': {'kind': 'which', 'name': name}, 'result': MACHINE['env']})
                return MACHINE['env']
            if name in ('hostname', 'scutil'):
                # The reference is replayed on every machine and belongs to
                # none of them: it records a host with no name of its own, so
                # the name of the machine that recorded it never enters a
                # public file. The privacy guard then looks for no machine,
                # and says so in what it prints.
                self.events.append({'call': {'kind': 'which', 'name': name}, 'result': None})
                return None
            if name != 'gh':
                raise AssertionError(f'unexpected tool lookup: {name}')
            value = original.which(name)
            if not value:
                raise RuntimeError('input recording requires authenticated gh')
            self.events.append({'call': {'kind': 'which', 'name': name}, 'result': value})
            return value
        def read(self, path):
            key, answer = responses.read(path, profile)
            self.events.append({'call': {'kind': 'read', 'path': path}, 'response': key})
            # As the replay gives it back, so that what is recorded is what
            # the program was told: a 404 with the words stored beside it.
            return replay.state_of(module, answer), copy.deepcopy(answer['body'])
        def run(self, command, cwd=None, env=None):
            if command == COMPOSED and cwd is None and env is None:
                self.events.append({'call': {'kind': 'run', 'argv': command, 'cwd': None},
                                    'result': {'returncode': 0, 'stdout': MACHINE['stdout'], 'stderr': ''}})
                return subprocess.CompletedProcess(command, 0, MACHINE['stdout'], '')
            if env is not None or cwd != project or not safe_git(command):
                raise AssertionError(f'unapproved recording process: {command!r}, {cwd}')
            done = original.run(command, cwd=cwd)
            event = {'call': {'kind': 'run', 'argv': command, 'cwd': str(cwd)},
                     'result': {'returncode': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr}}
            self.events.append(replay.substitute(event, {str(project): '<PRODUCT>', str(source): '<SOCLE>'}))
            return done
        def write(self, *args, **kwargs):
            raise AssertionError('recording forbids GitHub writes')
        def stream(self, *args, **kwargs):
            raise AssertionError('recording forbids streaming processes')
        def exec(self, *args, **kwargs):
            raise AssertionError('recording forbids process replacement')
        def feed(self, *args, **kwargs):
            raise AssertionError('recording forbids fed processes: check --history is not in the reference')
        def private_terms(self):
            return None  # the author's list is the author's, and a reference has no author
        def is_runner(self):
            return False  # nor is it any runner's: what it records is what an author's machine is asked
    return RecordingHost()


def provenance(commit, own_version, frozen_version):
    """Which commit was recorded, the VERSION it carries, and the VERSION it ran under.

    One member said both until a fix was recorded under a frozen version: the
    manifest then paired a commit at one version with the name of another,
    and nothing told the two apart.
    """
    return {'schemaVersion': 1, 'sourceCommit': commit, 'sourceVersion': own_version,
            'frozenVersion': frozen_version}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--source', type=pathlib.Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--responses-cache', type=pathlib.Path, required=True)
    parser.add_argument('--reference', type=pathlib.Path,
                        help='existing inputs whose frozen socle/VERSION and socle/CHANGELOG.md the recording keeps')
    parser.add_argument('--offline', action='store_true',
                        help='refuse a read the responses cache does not answer, instead of reading GitHub')
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if not re.fullmatch('[0-9a-f]{40}', args.commit) or git(source, 'rev-parse', 'HEAD') != args.commit:
        parser.error('--commit must name the full HEAD of the source')
    if git(source, 'status', '--porcelain') or output.exists():
        parser.error('source must be clean and output must not exist')
    if any(name.startswith('_MAELYS_RELEASE_') for name in os.environ):
        parser.error('record outside a self-test environment')
    if args.reference:
        # The reference gives the version the whole recording runs under: it
        # is held to what a replay holds its inputs to, and not to less.
        try:
            replay.verify_inputs(args.reference.resolve())
        except (OSError, KeyError, ValueError, RuntimeError) as refused:
            parser.error(f'--reference is not a verified set of inputs: {refused}')
    if args.offline and not args.responses_cache.is_file():
        parser.error('--offline needs the responses cache to exist: nothing else can answer a read')
    os.environ.update({'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'UTC', 'GH_HOST': 'github.com',
                       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
                       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0',
                       'MAELYS_RELEASE_NO_RELOCATE': '1'})
    with tempfile.TemporaryDirectory(prefix='api-record-') as directory:
        work = pathlib.Path(directory).resolve()
        stage = work / 'inputs'
        stage.mkdir()
        # A fix is recorded under the version its reference was frozen at:
        # the fixtures are built by the candidate's code, and their managed
        # headers carry that version. Recording the candidate's own VERSION
        # and CHANGELOG.md moved every fixture and every list of versions a
        # product is behind on, for three releases nobody had asked about.
        own_version = (source / 'VERSION').read_text(encoding='utf-8').strip()
        commit, source = args.commit, (replay.frozen_socle(source, args.reference.resolve(), work)
                                       if args.reference else source)
        module = replay.load_source(source)
        responses = Responses(replay.socle_host(module).Host(), args.responses_cache.resolve(), offline=args.offline)
        manifest = {**provenance(commit, own_version, module.socle_version()), 'cases': [], 'sha256': {}}
        for case in scenarios():
            folder = work / case['id']
            folder.mkdir()
            project = fixture(module, folder, case['repository'], args.commit, case['fixture'])
            files = replay.files_at(project)
            argv = [case['command'], str(project), *case['options']]
            if case['command'] in ('adopt', 'check', 'preflight'):
                argv += ['--socle-sha', args.commit, '--socle-tag', 'v' + module.socle_version()]
            if case['command'] == 'migrate':
                argv += ['--documents', str(project / 'documents.jsonl'), '--documents-repository', 'example/golden-docs']
            argv += ['--format', case['format']]
            host = recording_host(module, project, source, responses, case['profile'])
            result = replay.run_command(module, argv, project, source, host)
            path = 'cases/' + case['id'] + '.json'
            replay.write_json(stage / path, {'product': project.name, 'files': files,
                               'argv': replay.substitute(argv, {str(project): '<PRODUCT>', str(source): '<SOCLE>'}),
                               'events': host.events})
            manifest['cases'].append({**case, 'input': path})
            print(f"{case['id']}: exit {result['exitCode']}, {len(host.events)} host calls, {len(result['changedFiles'])} files changed", flush=True)
        replay.write_json(stage / 'responses.json', responses.used)
        for path in sorted(stage.rglob('*.json')):
            manifest['sha256'][path.relative_to(stage).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        # The version and the changelog are inputs, not code: frozen with the
        # cases, restored by the replay, so a later cut changes nothing here.
        for relative in replay.SOCLE_INPUTS:
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((source / pathlib.PurePosixPath(relative).name).read_bytes())
            manifest['sha256'][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        replay.write_json(stage / 'manifest.json', manifest)
        shutil.copytree(stage, output)
    print(f'Recorded {len(manifest["cases"])} cases; outputs must now be captured from the same source commit.')


if __name__ == '__main__':
    main()
