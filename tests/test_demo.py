import contextlib
import copy
import io
import unittest
from demo import Store, make_package, publish, select_artifact, validate_run, verify


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.m, self.f = make_package()
        self.store = Store()

    def test_complete_and_duplicate(self):
        events = publish(self.m, self.f, self.store)
        self.assertEqual([e['kind'] for e in events], ['upload'] * 3 + ['feed-complete'])
        before = copy.deepcopy(self.store.state['feeds'])
        self.assertEqual(publish(self.m, self.f, self.store)[0]['kind'], 'already-published-no-feed-write')
        self.assertEqual(before, self.store.state['feeds'])
        self.assertIn('files:', next(iter(before.values()))['yaml'])

    def test_interruption_rehydrated_retry(self):
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            publish(self.m, self.f, self.store, interrupt=True)
        self.assertEqual(len(self.store.state['objects']), 1)
        self.assertFalse(self.store.state['feeds'])
        resumed = Store(state=copy.deepcopy(self.store.state))
        events = publish(self.m, self.f, resumed)
        self.assertEqual([e['kind'] for e in events], ['skip-identical', 'upload', 'upload', 'feed-complete'])

    def test_conflicting_object_rejected(self):
        with self.assertRaises(RuntimeError):
            publish(self.m, self.f, self.store, interrupt=True)
        key = next(iter(self.store.state['objects']))
        self.store.state['objects'][key] = 'CONFLICT'
        with self.assertRaisesRegex(ValueError, 'immutable object conflict'):
            publish(self.m, self.f, self.store)
        self.assertFalse(self.store.state['feeds'])

    def test_newer_wins_both_serialized_orderings(self):
        newer, files = make_package(run='101', v='2.0.0')
        publish(newer, files, self.store)
        with self.assertRaisesRegex(ValueError, 'stale version'):
            publish(self.m, self.f, self.store)
        other = Store()
        publish(self.m, self.f, other)
        publish(newer, files, other)
        publish(self.m, self.f, other)
        self.assertEqual(next(iter(other.state['feeds'].values()))['version'], '2.0.0')

    def test_same_version_different_source_rejected(self):
        publish(self.m, self.f, self.store)
        other, files = make_package(run='101', sha='b' * 40)
        with self.assertRaisesRegex(ValueError, 'same version'):
            publish(other, files, self.store)

    def test_missing_extra_tampered_and_metadata(self):
        for change in ['missing', 'extra', 'tampered', 'destination', 'attempt', 'completion']:
            with self.subTest(change=change):
                m, f = copy.deepcopy(self.m), copy.deepcopy(self.f)
                if change == 'missing': del f['update.txt']
                if change == 'extra': f['evil.txt'] = b'x'
                if change == 'tampered': f['installer.txt'] += b'x'
                if change == 'destination': m['source']['channel'] = 'latest'
                if change == 'attempt': m['source']['attempt'] = '2'
                if change == 'completion': f['completion.json'] = b'{}'
                with self.assertRaises(ValueError): verify(m, f, '100', '1', 'a' * 40)

    def test_artifact_expired_missing_and_attempt_selection(self):
        artifacts = [dict(name='package-100-1', expired=False, id=1), dict(name='package-100-2', expired=False, id=2)]
        self.assertEqual(select_artifact(artifacts, '100', '2')['id'], 2)
        with self.assertRaisesRegex(ValueError, 'missing'): select_artifact(artifacts, '100', '3')
        artifacts[0]['expired'] = True
        with self.assertRaisesRegex(ValueError, 'expired'): select_artifact(artifacts, '100', '1')

    def test_failed_canceled_wrong_workflow_and_attempt(self):
        r = dict(id=100, workflow_id=1, path='.github/workflows/package.yml', event='workflow_dispatch',
                 head_branch='main', repository=dict(full_name='WangLeto/desktop-actions-approval-demo'),
                 run_attempt=1, status='completed', conclusion='success')
        validate_run(r, r, '100', '1', 1)
        for field, value in [('conclusion', 'failure'), ('conclusion', 'cancelled'), ('workflow_id', 2),
                             ('run_attempt', 2), ('head_branch', 'untrusted')]:
            bad = dict(r, **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_run(r, bad, '100', '1', 1)

    def test_explicit_old_attempt_does_not_follow_latest_failed_attempt(self):
        old = dict(id=100, workflow_id=1, path='.github/workflows/package.yml', event='workflow_dispatch',
                   head_branch='main', repository=dict(full_name='WangLeto/desktop-actions-approval-demo'),
                   run_attempt=1, status='completed', conclusion='success')
        latest = dict(old, run_attempt=2, conclusion='failure')
        validate_run(latest, old, '100', '1', 1)
        with self.assertRaisesRegex(ValueError, 'not successful'):
            validate_run(latest, latest, '100', '2', 1)

    def test_tag_failure_then_tag_only_after_newer_feed(self):
        with self.assertRaisesRegex(RuntimeError, 'fake tag API failure'):
            publish(self.m, self.f, self.store, production=True, fail_tag=True)
        newer, files = make_package(run='101', v='2.0.0')
        publish(newer, files, self.store, production=True)
        before = copy.deepcopy(self.store.state['feeds'])
        events = publish(self.m, self.f, self.store, production=True)
        self.assertEqual([e['kind'] for e in events], ['already-published-no-feed-write', 'tag-complete'])
        self.assertEqual(before, self.store.state['feeds'])
        self.assertEqual(self.store.state['tags']['synthetic-v1.0.0'], 'a' * 40)

    def test_unknown_write_readback_and_conflict(self):
        import base64
        import json
        state = dict(objects={}, feeds={}, completed={}, tags={}, events=[])
        lose = [True]
        def transport(path, method=None, body=None):
            nonlocal state
            if method == 'PUT':
                if lose[0]:
                    state = json.loads(base64.b64decode(body['content']))
                raise TimeoutError('response lost')
            return dict(sha='remote-sha', content=base64.b64encode(json.dumps(state).encode()).decode())
        store = Store(remote=True, transport=transport)
        store.save(dict(kind='durable'))
        self.assertEqual(store.reconciled_writes, 1)
        lose[0] = False
        with self.assertRaises(TimeoutError):
            store.save(dict(kind='not-committed'))
        self.assertEqual(state['events'], [dict(kind='durable')])

    def test_tag_conflict_never_moves(self):
        self.store.state['tags']['synthetic-v1.0.0'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'tag conflict'):
            publish(self.m, self.f, self.store, production=True)
        self.assertEqual(self.store.state['tags']['synthetic-v1.0.0'], 'b' * 40)


if __name__ == '__main__':
    unittest.main()
