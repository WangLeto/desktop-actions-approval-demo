"""Independent synthetic experiment; all remote writes target this demo repository."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

FILES = {'installer.txt', 'update.txt', 'completion.json'}
REPOSITORY = 'WangLeto/desktop-actions-approval-demo'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def version(value):
    require(isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value), 'invalid version')
    return tuple(map(int, value.split('.')))


def make_package(run='100', attempt='1', sha='a' * 40, v='1.0.0'):
    version(v)
    source = dict(repository=REPOSITORY, run=str(run), attempt=str(attempt), sha=sha,
                  version=v, platform='synthetic-linux', preset='test', channel='nightly',
                  test_release_id='synthetic-demo')
    files = {'installer.txt': f'FAKE INSTALLER {v} {sha}\n'.encode(),
             'update.txt': f'FAKE UPDATE {v} {sha}\n'.encode(),
             'completion.json': encoded(source)}
    manifest = dict(schema=1, source=source, files={name: dict(size=len(data), sha256=digest(data))
                                                for name, data in files.items()})
    return manifest, files


def validate_run(run, attempt, requested_run, requested_attempt, workflow_id):
    for data in [run, attempt]:
        require(str(data['id']) == str(requested_run), 'wrong run')
        require(data['workflow_id'] == workflow_id, 'wrong workflow')
        require(data['path'] == '.github/workflows/package.yml', 'wrong workflow path')
        require(data['event'] == 'workflow_dispatch' and data['head_branch'] == 'main', 'wrong source branch/event')
        require(data['repository']['full_name'] == REPOSITORY, 'wrong repository')
    require(str(attempt['run_attempt']) == str(requested_attempt), 'wrong attempt')
    require(attempt['status'] == 'completed' and attempt['conclusion'] == 'success', 'package attempt not successful')


def select_artifact(artifacts, run, attempt):
    matches = [a for a in artifacts if a['name'] == f'package-{run}-{attempt}']
    require(len(matches) == 1, 'missing or ambiguous artifact')
    require(not matches[0]['expired'], 'expired artifact')
    return matches[0]


def verify(manifest, files, run, attempt, sha):
    require(set(manifest) == {'schema', 'source', 'files'} and manifest['schema'] == 1, 'manifest schema')
    src = manifest['source']
    require(set(src) == {'repository', 'run', 'attempt', 'sha', 'version', 'platform', 'preset', 'channel', 'test_release_id'}, 'source schema')
    require(src['repository'] == REPOSITORY and src['run'] == str(run)
            and src['attempt'] == str(attempt) and src['sha'] == sha, 'source mismatch')
    require(re.fullmatch(r'[a-f0-9]{40}', sha), 'invalid source SHA')
    require((src['platform'], src['preset'], src['channel'], src['test_release_id']) ==
            ('synthetic-linux', 'test', 'nightly', 'synthetic-demo'), 'unexpected destination')
    version(src['version'])
    require(set(files) == FILES and set(manifest['files']) == FILES, 'incomplete or extra file inventory')
    for name, data in files.items():
        require(manifest['files'][name] == dict(size=len(data), sha256=digest(data)), f'changed file: {name}')
    require(json.loads(files['completion.json']) == src, 'completion mismatch')


def api(path, method=None, body=None, raw=False):
    args = ['gh', 'api', path]
    if method:
        args += ['-X', method]
    if body is not None:
        args += ['--input', '-']
    data = subprocess.check_output(args, input=encoded(body) if body is not None else None)
    return data if raw else json.loads(data)


def summary(text):
    print(text)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(text + '\n')


def fetch_package():
    require(os.environ['GITHUB_REPOSITORY'] == REPOSITORY, 'unexpected execution repository')
    run_id = os.environ['PACKAGE_RUN']
    require(re.fullmatch(r'[1-9]\d*', run_id), 'invalid run ID')
    root = f'repos/{REPOSITORY}'
    run = api(f'{root}/actions/runs/{run_id}')
    attempt_id = os.getenv('PACKAGE_ATTEMPT') or str(run['run_attempt'])
    require(re.fullmatch(r'[1-9]\d*', attempt_id), 'invalid attempt')
    attempt = api(f'{root}/actions/runs/{run_id}/attempts/{attempt_id}')
    workflow = api(f'{root}/actions/workflows/package.yml')
    validate_run(run, attempt, run_id, attempt_id, workflow['id'])
    artifacts = []
    page = 1
    while True:
        batch = api(f'{root}/actions/runs/{run_id}/artifacts?per_page=100&page={page}')['artifacts']
        artifacts.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    artifact = select_artifact(artifacts, run_id, attempt_id)
    require(artifact['size_in_bytes'] < 1000000, 'artifact too large for synthetic demo')
    data = api(f"{root}/actions/artifacts/{artifact['id']}/zip", raw=True)
    require(artifact.get('digest') == 'sha256:' + digest(data), 'archive digest mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(len(archive.infolist()) == 4 and set(archive.namelist()) == FILES | {'manifest.json'}, 'unexpected archive inventory')
        require(sum(i.file_size for i in archive.infolist()) < 100000, 'expanded package too large')
        files = {name: archive.read(name) for name in FILES}
        manifest = json.loads(archive.read('manifest.json'))
    verify(manifest, files, run_id, attempt_id, attempt['head_sha'])
    environment = api(f'{root}/environments/demo-test')
    require(any(p['type'] == 'required_reviewers' and p.get('reviewers') for p in environment['protection_rules']), 'required reviewers missing')
    approval = digest(encoded(dict(manifest=manifest, artifact_id=artifact['id'], archive_digest=artifact['digest'],
                                   destination='demo-store:state.json/test/nightly/synthetic-demo')))
    return manifest, files, approval


class Store:
    def __init__(self, remote=False, state=None, path="state.json", transport=api):
        self.remote = remote
        require(re.fullmatch(r"(?:state|probe-[a-z0-9-]+)\.json", path), "invalid store path")
        self.path = path
        self.transport = transport
        self.reconciled_writes = 0
        self.state = state or dict(objects={}, feeds={}, completed={}, tags={}, events=[])
        self.sha = None
        if remote:
            record = self.transport(f'repos/{REPOSITORY}/contents/{self.path}?ref=demo-store')
            self.sha = record['sha']
            self.state = json.loads(base64.b64decode(record['content']))

    def save(self, event):
        self.state['events'].append(event)
        print(json.dumps(event, sort_keys=True))
        if self.remote:
            try:
                result = self.transport(f'repos/{REPOSITORY}/contents/{self.path}', 'PUT', dict(
                    message='Record synthetic publication step', branch='demo-store', sha=self.sha,
                    content=base64.b64encode(encoded(self.state)).decode()))
                self.sha = result['content']['sha']
            except (subprocess.CalledProcessError, TimeoutError):
                # The remote write may have committed even though its response was lost.
                # Only exact readback proves this write; a different state stays a failure.
                record = self.transport(f'repos/{REPOSITORY}/contents/{self.path}?ref=demo-store')
                actual = json.loads(base64.b64decode(record['content']))
                if actual != self.state:
                    raise
                self.sha = record['sha']
                self.reconciled_writes += 1
                print('Reconciled unknown write outcome using exact remote readback')


def publish(manifest, files, store, interrupt=False, production=False, fail_tag=False):
    src = manifest['source']
    verify(manifest, files, src['run'], src['attempt'], src['sha'])
    state = store.state
    identity = digest(encoded(manifest))
    channel = f"{src['preset']}/{src['channel']}/{src['test_release_id']}/{src['platform']}"
    events = []

    def record(kind, **details):
        event = dict(kind=kind, package=identity, **details)
        events.append(event)
        store.save(event)

    def tag():
        # Local fake response transport only. No remote tag API exists in this demo.
        require(not store.remote, 'production tags may only be tested locally')
        if fail_tag:
            record('tag-pending')
            raise RuntimeError('fake tag API failure')
        name = 'synthetic-v' + src['version']
        require(state['tags'].get(name, src['sha']) == src['sha'], 'tag conflict')
        state['tags'][name] = src['sha']
        record('tag-complete', tag=name)

    if identity in state['completed']:
        record('already-published-no-feed-write')
        if production:
            tag()
        return events
    previous = state['feeds'].get(channel)
    if previous:
        require(version(previous['version']) <= version(src['version']), 'stale version cannot replace newer feed')
        require(previous['version'] != src['version'] or previous['package'] == identity, 'same version has different package')
    for name in sorted(files):
        key = f"{channel}/{src['version']}/{name}"
        content = base64.b64encode(files[name]).decode()
        require(key not in state['objects'] or state['objects'][key] == content, 'immutable object conflict')
        if key in state['objects']:
            record('skip-identical', key=key)
        else:
            state['objects'][key] = content
            record('upload', key=key)
            if interrupt:
                raise RuntimeError('simulated interruption after durable first object')
    yaml = f"version: {src['version']}\nfiles:\n" + ''.join(
        f"  - url: {src['version']}/{name}\n    sha256: {digest(files[name])}\n" for name in sorted(files))
    state['feeds'][channel] = dict(version=src['version'], package=identity, yaml=yaml)
    state['completed'][identity] = dict(channel=channel, version=src['version'])
    record('feed-complete', version=src['version'])
    if production:
        tag()
    return events


def main():
    mode = sys.argv[1]
    if mode == 'package':
        manifest, files = make_package(os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'],
                                      os.environ['GITHUB_SHA'], os.environ['VERSION'])
        fixture = os.getenv('FIXTURE')
        if fixture == 'tampered':
            files['installer.txt'] += b'TAMPERED'
        if fixture == 'missing':
            del files['update.txt']
        destination = Path('package')
        destination.mkdir()
        for name, data in files.items():
            (destination / name).write_bytes(data)
        (destination / 'manifest.json').write_bytes(encoded(manifest))
        summary('Package retained; no publication requested.\n```json\n' + json.dumps(manifest, indent=2) + '\n```')
        return
    manifest, files, approval = fetch_package()
    src = manifest['source']
    summary(f"## Synthetic publication approval\n\nPackage **{src['run']} / attempt {src['attempt']}**\n\n"
            f"Version **{src['version']}**, test / nightly / synthetic-linux\n\nSource `{src['sha']}`\n\n"
            f"Destination: demo-store branch, state.json; no real upload or tag.\n\nApproved digest `{approval}`\n\n"
            + '| File | Bytes | SHA-256 |\n|---|---:|---|\n' + ''.join(
                f"| {n} | {m['size']} | {m['sha256']} |\n" for n, m in manifest['files'].items()))
    if mode == 'prepare':
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write(f"attempt={src['attempt']}\napproval={approval}\nlabel={src['version']} run {src['run']} attempt {src['attempt']}\n")
    elif mode == 'publish':
        require(os.environ['APPROVED_DIGEST'] == approval, 'package changed after approval')
        if os.getenv("SCENARIO") == "hold_60":
            print("Holding the publisher concurrency slot for 60 seconds", flush=True)
            time.sleep(60)
        store = Store(remote=True)
        interrupt = os.getenv('SCENARIO') == 'interrupt_once' and os.environ['GITHUB_RUN_ATTEMPT'] == '1'
        try:
            events = publish(manifest, files, store, interrupt=interrupt)
            summary('Publication **complete**, including YAML feed.\n```json\n' + json.dumps(events, indent=2) + '\n```')
        except Exception:
            summary('Publication **failed**; persisted state must be reconciled on retry.')
            raise
    else:
        raise ValueError('unknown mode')


if __name__ == '__main__':
    main()
