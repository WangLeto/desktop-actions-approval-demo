"""Real GitHub CAS plus injected lost responses on isolated synthetic state."""
import base64
import json
import subprocess
import time
from demo import Store, api, encoded, make_package, publish, REPOSITORY

path = 'probe-cas-' + str(time.time_ns()) + '.json'
initial = dict(objects={}, feeds={}, completed={}, tags={}, events=[])
api(f'repos/{REPOSITORY}/contents/{path}', 'PUT', dict(message='Initialize isolated CAS probe',
    branch='demo-store', content=base64.b64encode(encoded(initial)).decode()))
winner, loser = Store(remote=True, path=path), Store(remote=True, path=path)
winner.save(dict(kind='winner'))
try:
    loser.save(dict(kind='stale-writer'))
    raise AssertionError('stale CAS unexpectedly succeeded')
except subprocess.CalledProcessError as error:
    assert b'409' in error.output, error.output
    print('PASS: real GitHub HTTP 409 rejected stale writer')
assert Store(remote=True, path=path).state == winner.state
lost = {'count': 0}
def drop_response(endpoint, method=None, body=None, raw=False):
    result = api(endpoint, method, body, raw)
    if method == 'PUT':
        lost['count'] += 1
        raise TimeoutError('injected loss AFTER actual remote commit, before caller sees response')
    return result
store = Store(remote=True, path=path, transport=drop_response)
m, f = make_package(run='999', v='99.0.0')
events = publish(m, f, store)
assert lost['count'] == store.reconciled_writes == 4
actual = Store(remote=True, path=path)
assert actual.state == store.state
assert len(actual.state['objects']) == 3 and len(actual.state['feeds']) == 1
print('PASS: 3 objects and feed reconciled after injected response loss')
print(json.dumps(dict(path=path, actual_github_cas_conflict=True, injected_lost_responses=lost['count'],
    reconciled_writes=store.reconciled_writes, events=events), indent=2))
