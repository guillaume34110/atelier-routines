"""Real Git, stdio and HTTP scenarios; credentials and repositories are synthetic."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

import executor
import github_ops
from test_mcp import command, connection, make_fixture, rpc

HEAD = 'a' * 40
BASE = 'b' * 40
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys, urllib.request, urllib.error
args = sys.argv[1:]
settings = json.load(open(os.environ['ATELIER_ROUTINES_TEST_AUTH']))
if args[:2] == ['auth', 'token']:
    assert 'GH_TOKEN' not in os.environ and 'GITHUB_TOKEN' not in os.environ
    user = args[args.index('--user') + 1]
    if user in settings.get('missing', []):
        sys.exit(1)
    print('fixture-' + settings.get('wrong_user', user))
elif args and args[0] == 'api':
    endpoint = args[args.index('--method') + 2]
    method = args[args.index('--method') + 1]
    payload = sys.stdin.buffer.read() if '--input' in args else None
    request = urllib.request.Request(os.environ['ATELIER_ROUTINES_TEST_API'] + '/' + endpoint,
        data=payload, method=method, headers={'Authorization': os.environ.get('GH_TOKEN', '')})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            print(response.read().decode())
    except urllib.error.HTTPError as error:
        print('HTTP ' + str(error.code))
        sys.exit(1)
else:
    sys.exit(2)
'''


@contextmanager
def service(project, owner):
    state = {'owner': owner, 'requests': [], 'prs': [], 'posts': 0, 'merges': 0, 'pending': False,
             'failure': False, 'missing': False, 'behind': 0, 'protected': True, 'admin': True,
             'head': HEAD, 'uncertain_create': False, 'uncertain_merge': False, 'app_id': 1,
             'push': True, 'rules': [], 'ruleset': {}}
    name = owner + '/fixture'

    def item():
        return {'number': 1, 'title': 'Fixture', 'html_url': 'https://github.com/' + name + '/pull/1',
                'state': 'open', 'merged': False, 'draft': False,
                'head': {'sha': state['head'], 'ref': 'work', 'repo': {'full_name': name}},
                'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': name}}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, status=200):
            content = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            login = self.headers.get('Authorization', '').removeprefix('fixture-')
            path = urlsplit(self.path).path
            state['requests'].append((self.command, path, login))
            if path == '/user':
                return self.send({'login': login})
            if path.startswith('/search/issues'):
                return self.send({'total_count': 0, 'incomplete_results': False, 'items': []})
            if login != owner:
                return self.send({}, 403)
            prefix = '/repos/' + name
            if path == prefix:
                return self.send({'full_name': name, 'default_branch': 'main', 'permissions': {'push': state['push'], 'pull': True, 'admin': True}})
            if path.endswith('/protection'):
                if not state['protected']:
                    return self.send({}, 404)
                return self.send({'required_pull_request_reviews': {}, 'enforce_admins': {'enabled': state['admin']},
                                  'required_status_checks': {'strict': True, 'contexts': ['verify', 'security'],
                                  'checks': [{'context': n, 'app_id': 1} for n in ('verify', 'security')]}})
            if '/rules/branches/' in path:
                return self.send(state['rules'])
            if '/rulesets/' in path:
                return self.send(state['ruleset'])
            if path.endswith('/check-runs'):
                names = ['verify'] if state['missing'] else ['verify', 'security']
                rows = [{'id': i, 'name': n, 'status': 'in_progress' if state['pending'] else 'completed',
                         'conclusion': 'failure' if state['failure'] else 'success',
                         'head_sha': state['head'], 'app_id': state['app_id']} for i, n in enumerate(names, 1)]
                return self.send({'total_count': len(rows), 'check_runs': rows})
            if path.endswith('/status'):
                return self.send({'total_count': 0, 'statuses': []})
            if '/compare/' in path:
                return self.send({'behind_by': state['behind'], 'base_sha': BASE})
            if '/git/ref/heads/' in path:
                return self.send({'sha': state['head']})
            if path.endswith('/pulls/1'):
                return self.send(state['prs'][0] if state['prs'] else item())
            if path.endswith('/pulls'):
                return self.send(state['prs'])
            self.send({}, 404)

        def do_POST(self):
            login = self.headers.get('Authorization', '').removeprefix('fixture-')
            state['requests'].append((self.command, self.path, login))
            if login != owner:
                return self.send({}, 403)
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['posts'] += 1
            created = item()
            created['title'] = data['title']
            state['prs'].append(created)
            self.send(created, 500 if state['uncertain_create'] else 200)

        def do_PUT(self):
            login = self.headers.get('Authorization', '').removeprefix('fixture-')
            state['requests'].append((self.command, self.path, login))
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if login != owner or data['sha'] != state['head']:
                return self.send({}, 409)
            state['merges'] += 1
            if not state['prs']:
                state['prs'].append(item())
            state['prs'][0].update(merged=True, state='closed')
            self.send({'merged': True, 'sha': 'c' * 40}, 500 if state['uncertain_merge'] else 200)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    directory = project.parent / 'fake-bin'
    directory.mkdir()
    (directory / 'gh').write_text(FAKE_GH)
    (directory / 'gh').chmod(0o700)
    auth = project.parent / 'auth.json'
    auth.write_text('{}')
    try:
        yield state, auth, {'PATH': str(directory) + os.pathsep + os.environ['PATH'],
                            'ATELIER_ROUTINES_TEST_AUTH': str(auth),
                            'ATELIER_ROUTINES_TEST_API': 'http://127.0.0.1:' + str(server.server_port)}
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class GithubTest(unittest.TestCase):
    def test_owner_policy_and_conflicting_override(self):
        self.assertEqual(github_ops.account_for('Guillaume34110'), 'guillaume34110')
        self.assertEqual(github_ops.account_for('guillaume-rebaze'), 'guillaume-rebaze')
        self.assertEqual(github_ops.account_for('organization', ['guillaume-rebaze']), 'guillaume-rebaze')
        for owner, configured in [('guillaume34110', ['guillaume-rebaze']), ('unknown', []), ('unknown', ['other'])]:
            with self.subTest(owner=owner, configured=configured), self.assertRaises(executor.Refusal):
                github_ops.account_for(owner, configured)

    def test_real_accounts_and_transport(self):
        asyncio.run(self.check_accounts())

    async def check_accounts(self):
        for owner in github_ops.ACCOUNTS:
            with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
                project = make_fixture(tmp)
                command(project, 'git', 'remote', 'add', 'origin', 'https://github.com/' + owner + '/fixture.git')
                with service(project, owner) as (state, auth, env):
                    async with connection(project, **env, GH_TOKEN='fixture-wrong', GITHUB_TOKEN='fixture-wrong') as session:
                        body, _ = await rpc(session, 'routines_run', {'id': 'github.context', 'params': {'project': str(project)}})
                        self.assertEqual(body['status'], 'ok', body)
                        proof = json.loads(Path(body['evidence'][0]).read_text())
                        self.assertEqual(proof['result']['summary']['account'], owner)
                        self.assertEqual({r[2] for r in state['requests']}, {owner})
                        self.assertNotIn('fixture-' + owner, json.dumps(proof))
                        self.assertEqual(command(project, 'git', 'config', 'user.name').strip(), 'Fixture')
                        auth.write_text(json.dumps({'wrong_user': 'wrong-account'}))
                        bad, _ = await rpc(session, 'routines_run', {'id': 'github.context', 'params': {'project': str(project)}})
                        self.assertEqual(bad['status'], 'decision_required', bad)
                        auth.write_text(json.dumps({'missing': [owner]}))
                        missing, _ = await rpc(session, 'routines_run', {'id': 'github.context', 'params': {'project': str(project)}})
                        self.assertEqual(missing['status'], 'unavailable', missing)
                        combined, _ = await rpc(session, 'routines_run', {'id': 'github.prs', 'params': {'project': str(project), 'scope': 'accounts'}})
                        self.assertEqual(combined['status'], 'partial', combined)

    def test_pr_lifecycle_and_known_execution_wait(self):
        asyncio.run(self.check_lifecycle())

    async def check_lifecycle(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            owner = 'guillaume34110'
            command(project, 'git', 'remote', 'add', 'origin', 'https://github.com/' + owner + '/fixture.git')
            with service(project, owner) as (state, auth, env):
                async with connection(project, **env) as session:
                    async def call(identifier, **params):
                        body, _ = await rpc(session, 'routines_run', {'id': identifier, 'params': {'project': str(project), **params}})
                        return body
                    created = await call('git.pr-create', branch='work', head_sha=HEAD, title='Fixture')
                    self.assertEqual(created['status'], 'ok', created)
                    self.assertEqual(state['posts'], 1)
                    again = await call('git.pr-create', branch='work', head_sha=HEAD, title='Fixture')
                    self.assertEqual(again['status'], 'ok', again)
                    self.assertEqual(state['posts'], 1)
                    state['pending'] = True
                    pending = await call('git.pr-checks', number=1, head_sha=HEAD)
                    self.assertEqual(pending['status'], 'partial', pending)
                    state['pending'] = False
                    ready = await call('executions.wait', execution_id=pending['execution_id'], seconds=1)
                    self.assertEqual(ready['status'], 'ok', ready)
                    self.assertEqual(ready['execution_id'], pending['execution_id'])
                    for key, value in [('head', 'd' * 40), ('failure', True), ('missing', True), ('behind', 1), ('admin', False), ('protected', False), ('app_id', 999), ('push', False)]:
                        original = state[key]
                        state[key] = value
                        blocked = await call('git.merge', number=1, head_sha=HEAD)
                        self.assertNotEqual(blocked['status'], 'ok', (key, blocked))
                        self.assertEqual(state['merges'], 0)
                        state[key] = original
                    state['protected'] = False
                    state['rules'] = [{'type': 'pull_request', 'ruleset_id': 1}, {'type': 'required_status_checks', 'ruleset_id': 1,
                        'parameters': {'strict_required_status_checks_policy': True, 'required_status_checks': [
                            {'context': 'verify', 'integration_id': 1}, {'context': 'security', 'integration_id': 1}]}}]
                    state['ruleset'] = {'enforcement': 'active', 'bypass_actors': []}
                    policy = await call('git.integration', number=1, head_sha=HEAD)
                    self.assertEqual(policy['status'], 'ok', policy)
                    state['ruleset']['bypass_actors'] = [{'actor_type': 'OrganizationAdmin', 'bypass_mode': 'always'}]
                    bypass = await call('git.merge', number=1, head_sha=HEAD)
                    self.assertNotEqual(bypass['status'], 'ok', bypass)
                    self.assertEqual(state['merges'], 0)
                    state['protected'], state['rules'] = True, []
                    merged = await call('git.merge', number=1, head_sha=HEAD)
                    self.assertEqual(merged['status'], 'ok', merged)
                    self.assertEqual(state['merges'], 1)
                    self.assertEqual({r[2] for r in state['requests']}, {owner})

    def test_uncertain_writes_resume_without_resubmission(self):
        asyncio.run(self.check_uncertainty())

    async def check_uncertainty(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            owner = 'guillaume-rebaze'
            command(project, 'git', 'remote', 'add', 'origin', 'https://github.com/' + owner + '/fixture.git')
            with service(project, owner) as (state, auth, env):
                async with connection(project, **env) as session:
                    state['uncertain_create'] = True
                    created, _ = await rpc(session, 'routines_run', {'id': 'git.pr-create', 'params': {'project': str(project), 'branch': 'work', 'head_sha': HEAD, 'title': 'Fixture'}, 'request_id': 'create-once'})
                    self.assertEqual(created['status'], 'decision_required', created)
                    blocked, _ = await rpc(session, 'routines_run', {'id': 'github.context', 'params': {'project': str(project)}})
                    self.assertEqual(blocked['status'], 'decision_required', blocked)
                    self.assertEqual(blocked['execution_id'], created['execution_id'])
                    self.assertEqual(state['posts'], 1)
                    resumed, _ = await rpc(session, 'routines_run', {'id': 'executions.resume', 'params': {'project': str(project), 'execution_id': created['execution_id']}})
                    self.assertEqual(resumed['status'], 'ok', resumed)
                    self.assertEqual(created['execution_id'], resumed['execution_id'])
                    self.assertEqual(state['posts'], 1)
                    state['uncertain_merge'] = True
                    merged, _ = await rpc(session, 'routines_run', {'id': 'git.merge', 'params': {'project': str(project), 'number': 1, 'head_sha': HEAD}})
                    self.assertEqual(merged['status'], 'decision_required', merged)
                    recovered, _ = await rpc(session, 'routines_run', {'id': 'executions.resume', 'params': {'project': str(project), 'execution_id': merged['execution_id']}})
                    self.assertEqual(recovered['status'], 'ok', recovered)
                    self.assertEqual(state['merges'], 1)

    def test_distinct_push_target_and_write_recipe_refused(self):
        asyncio.run(self.check_destinations())

    async def check_destinations(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            owner = 'guillaume34110'
            command(project, 'git', 'remote', 'add', 'origin', 'https://github.com/' + owner + '/fixture.git')
            command(project, 'git', 'remote', 'set-url', '--push', 'origin', 'https://github.com/guillaume-rebaze/fixture.git')
            with service(project, owner) as (state, auth, env):
                async with connection(project, **env) as session:
                    remote, _ = await rpc(session, 'routines_run', {'id': 'git.remote', 'params': {'project': str(project)}})
                    proof = json.loads(Path(remote['evidence'][0]).read_text())
                    self.assertEqual({r['direction'] for r in proof['result']['details']}, {'fetch', 'push'})
                    self.assertEqual(len(proof['result']['details']), 2)
                    refused, _ = await rpc(session, 'routines_run', {'id': 'git.pr-create', 'params': {'project': str(project), 'branch': 'work', 'head_sha': HEAD, 'title': 'Fixture'}})
                    self.assertEqual(refused['status'], 'decision_required', refused)
                    self.assertEqual(state['posts'], 0)
                    recipe, _ = await rpc(session, 'routines_run', {'id': 'routines.register', 'params': {'project': str(project), 'name': 'no-write', 'description': 'Fixture',
                        'steps': [{'id': 'git.merge', 'params': {'number': 1, 'head_sha': HEAD}}]}})
                    self.assertEqual(recipe['status'], 'error', recipe)

    def test_compact_keeps_structured_details(self):
        record = {'id': 'fixture', 'status': 'partial', 'result': {'summary': {'matches': 100}, 'partial': True,
                  'details': [{'name': 'value-' + str(i), 'text': 'x' * 120} for i in range(100)]}, 'evidence': []}
        compact = executor.compact(record)
        self.assertLessEqual(len(json.dumps(compact).encode()), 1950)
        self.assertTrue(compact['response_truncated'])
        self.assertIsInstance(compact['result']['details'], list)
        self.assertTrue(compact['result']['partial'])
        self.assertTrue(compact['result']['has_more'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
