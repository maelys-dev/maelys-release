#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Replay frozen API/host inputs against a candidate, offline (Python 3.9+).

    python3 tests/golden/capture_api.py /tmp/api-replay
    diff -ru tests/golden/api/baseline /tmp/api-replay

Every command runs in a fresh interpreter through its real agent-cli entry
point. Only host.HOST is replaced. No subprocess, network operation or write
through that host can fall back to the real machine. Product files are copied
from frozen inputs; only explicitly recorded adopt cases may change them.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
INPUTS = pathlib.Path(__file__).parent / 'api' / 'inputs'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def load_source(source):
    loader = importlib.machinery.SourceFileLoader('api_golden_candidate', str(source / 'bin/maelys-release'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def substitute(value, replacements):
    if isinstance(value, str):
        for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, replacements) for key, item in value.items()}
    return value


def files_at(project):
    files = {}
    for path in sorted(project.rglob('*')):
        relative = path.relative_to(project)
        if '.git' in relative.parts:
            continue
        if path.is_symlink():
            raise RuntimeError(f'fixture symlink not supported: {relative}')
        if path.is_file():
            files[relative.as_posix()] = {'text': path.read_text(encoding='utf-8'),
                                         'executable': bool(path.stat().st_mode & 0o111)}
    return files


def restore_files(project, files):
    project.mkdir()
    (project / '.git').mkdir()  # Presence is an input too; git itself is never run on replay.
    for relative, item in files.items():
        path = pathlib.PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
            raise RuntimeError(f'unsafe fixture path: {relative}')
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item['text'], encoding='utf-8')
        target.chmod(0o755 if item['executable'] else 0o644)


def validate_argv(argv):
    if not argv or argv[0] not in ('protect', 'preflight', 'check', 'adopt', 'migrate'):
        raise RuntimeError('only recorded inspection commands may run')
    flags = {arg.split('=', 1)[0] for arg in argv}
    if '--push' in flags or ('--apply' in flags and argv[0] != 'adopt'):
        raise RuntimeError('remote writes are forbidden, including during input recording')


def run_command(module, argv, project, source, host):
    validate_argv(argv)
    paths = {str(project): '<PRODUCT>', str(source): '<SOCLE>'}
    before = files_at(project)
    out, err = io.StringIO(), io.StringIO()
    previous = module.host.HOST
    module.host.HOST = host
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.APP.main(argv)
    finally:
        module.host.HOST = previous
    after = files_at(project)
    if '--apply' not in argv and before != after:
        raise RuntimeError('a plan modified the fixture')
    if code not in (0, 1, 2):
        raise RuntimeError(f'non-contract exit: {code}')
    if '--format' in argv and argv[argv.index('--format') + 1] == 'json':
        envelope = json.loads(err.getvalue() if code == 1 else out.getvalue())
        if envelope.get('contract') != 'agent-cli/v2' or envelope.get('exitCode') != code:
            raise RuntimeError('not an agent-cli/v2 envelope')
    changes = {name: after.get(name) for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)}
    return substitute({'exitCode': code, 'stdout': out.getvalue(), 'stderr': err.getvalue(),
                       'changedFiles': changes}, paths)


def replay_host(module, events, responses, project, source):
    class ReplayHost(module.Host):
        def __init__(self):
            self.position = 0

        def consume(self, call):
            call = substitute(call, {str(project): '<PRODUCT>', str(source): '<SOCLE>'})
            if self.position >= len(events) or events[self.position]['call'] != call:
                expected = events[self.position]['call'] if self.position < len(events) else '<end>'
                raise AssertionError(f'unrecorded host call: {call!r}; expected {expected!r}')
            event = events[self.position]
            self.position += 1
            return event

        def which(self, name):
            return self.consume({'kind': 'which', 'name': name})['result']

        def read(self, path):
            event = self.consume({'kind': 'read', 'path': path})
            reply = responses[event['response']]
            return reply['state'], json.loads(json.dumps(reply['body']))

        def run(self, command, cwd=None, env=None):
            if env is not None:
                raise AssertionError('unrecorded process environment')
            event = self.consume({'kind': 'run', 'argv': command, 'cwd': str(cwd) if cwd else None})
            answer = substitute(event['result'], {'<PRODUCT>': str(project), '<SOCLE>': str(source)})
            return subprocess.CompletedProcess(command, answer['returncode'], answer['stdout'], answer['stderr'])

        def write(self, *args, **kwargs):
            raise AssertionError('API golden forbids GitHub writes')

        def stream(self, *args, **kwargs):
            raise AssertionError('API golden forbids streaming processes')

        def exec(self, *args, **kwargs):
            raise AssertionError('API golden forbids process replacement')

        def finish(self):
            if self.position != len(events):
                raise AssertionError(f'{len(events) - self.position} recorded host calls were not consumed')

    return ReplayHost()


def worker(source, inputs, case_name, output):
    manifest = read_json(inputs / 'manifest.json')
    spec = next(case for case in manifest['cases'] if case['id'] == case_name)
    case = read_json(inputs / spec['input'])
    with tempfile.TemporaryDirectory(prefix='api-case-') as directory:
        project = pathlib.Path(directory).resolve() / case['product']
        restore_files(project, case['files'])
        module = load_source(source)
        host = replay_host(module, case['events'], read_json(inputs / 'responses.json'), project, source)
        argv = substitute(case['argv'], {'<PRODUCT>': str(project), '<SOCLE>': str(source)})
        result = run_command(module, argv, project, source, host)
        host.finish()
        write_json(output, result)


def verify_inputs(inputs):
    manifest = read_json(inputs / 'manifest.json')
    if manifest['schemaVersion'] != 1 or not manifest['cases']:
        raise RuntimeError('unsupported or empty API reference')
    ids = [case['id'] for case in manifest['cases']]
    if any(not re.fullmatch('[a-z0-9-]+', name) for name in ids) or len(set(ids)) != len(ids):
        raise RuntimeError('invalid or duplicate case id')
    expected = {'responses.json', *(case['input'] for case in manifest['cases'])}
    if set(manifest['sha256']) != expected:
        raise RuntimeError('every input must have a frozen digest')
    for relative, digest in manifest['sha256'].items():
        path = pathlib.PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts:
            raise RuntimeError(f'unsafe input path: {relative}')
        if hashlib.sha256((inputs / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'frozen input digest differs: {relative}')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--source', type=pathlib.Path, default=ROOT)
    parser.add_argument('--inputs', type=pathlib.Path, default=INPUTS)
    parser.add_argument('--case', action='append', help='replay only the named case (repeatable)')
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    args = parser.parse_args()
    source, inputs, output = args.source.resolve(), args.inputs.resolve(), args.output.resolve()
    if args.worker:
        worker(source, inputs, args.worker, output)
        return
    manifest = verify_inputs(inputs)
    if output.exists():
        parser.error(f'refusing to overwrite {output}')
    selected = [case for case in manifest['cases'] if not args.case or case['id'] in args.case]
    if args.case and set(args.case) != {case['id'] for case in selected}:
        parser.error('unknown case')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='api-golden-') as directory:
        work = pathlib.Path(directory).resolve()
        stage = work / 'output'
        stage.mkdir()
        # The candidate inherits no credentials, self-test switches or executable PATH.
        # The only processes are these fresh Python interpreters, started by the driver.
        environment = {'PATH': '', 'HOME': str(work), 'TMPDIR': str(work), 'XDG_CACHE_HOME': str(work / 'cache'),
                       'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'UTC', 'PYTHONHASHSEED': '0',
                       'PYTHONDONTWRITEBYTECODE': '1', 'MAELYS_RELEASE_NO_RELOCATE': '1'}
        for case in selected:
            completed = subprocess.run([sys.executable, '-I', str(pathlib.Path(__file__).resolve()),
                                       str(stage / (case['id'] + '.json')), '--source', str(source),
                                       '--inputs', str(inputs), '--worker', case['id']],
                                      env=environment, cwd=work, capture_output=True, text=True, timeout=60)
            if completed.returncode:
                raise RuntimeError(f"{case['id']}: {completed.stderr}")
        write_json(stage / 'manifest.json', {'schemaVersion': 1, 'sourceCommit': manifest['sourceCommit'],
                                            'inputsSha256': hashlib.sha256((inputs / 'manifest.json').read_bytes()).hexdigest(),
                                            'cases': [case['id'] for case in selected]})
        shutil.copytree(stage, output)
    print(f'Replayed {len(selected)} API/host cases offline into {output}')


if __name__ == '__main__':
    main()
