"""Real stdio tests on synthetic projects; TASK_WORKSPACE is supplied by the guard."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import executor

HERE = Path(__file__).resolve().parent
RTK = shutil.which('rtk')


def command(project, *argv):
    result = subprocess.run([RTK, 'proxy', *argv], cwd=project, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr[:500])
    return result.stdout


def make_fixture(parent):
    project = Path(parent) / 'project with spaces $literal'
    project.mkdir()
    (project / 'src').mkdir()
    (project / 'src/sample.py').write_text('def calculate(x):\n    return x + 1\n\nclass Sample:\n    pass\n')
    (project / 'package.json').write_text(json.dumps({'scripts': {'test': 'python -m unittest', 'build': 'unused'}}))
    (project / 'data.json').write_text(json.dumps({'items': [1, 2], 'name': 'fixture'}))
    (project / 'data.csv').write_text('name,value\na,1\nb,\n')
    (project / 'events.log').write_text('INFO ready\nERROR connection timeout token=synthetic-value\nWARNING retry disabled\n')
    (project / '.env').write_text('calculate=must-not-appear\n')
    (project / '.gitignore').write_text('output/\n.env\n')
    (project / '.github/workflows').mkdir(parents=True)
    (project / '.github/workflows/check.yml').write_text('name: fixture\n')
    command(project, 'git', 'init', '--quiet')
    command(project, 'git', 'config', 'user.name', 'Fixture')
    command(project, 'git', 'config', 'user.email', 'fixture@example.invalid')
    command(project, 'git', 'add', '.')
    command(project, 'git', 'commit', '--quiet', '-m', 'fixture')
    (project / 'src/sample.py').write_text((project / 'src/sample.py').read_text() + '\n# local change\n')
    (project / 'staged.txt').write_text('staged\n')
    command(project, 'git', 'add', 'staged.txt')
    return project


@asynccontextmanager
async def connection(project, **overrides):
    env = {'PATH': os.environ['PATH'], 'ATELIER_ROUTINES_ROOTS': json.dumps([str(project)]),
           'ATELIER_ROUTINES_STORE': str(project.parent / (project.name + '-definitions')),
           'PYTHONDONTWRITEBYTECODE': '1',
           **{k: v for k, v in os.environ.items() if k.startswith('USEVPS_')}, **overrides}
    params = StdioServerParameters(command=RTK, args=['proxy', sys.executable, str(HERE / 'mcp_server.py')], env=env)
    with (Path(os.environ['TASK_WORKSPACE']) / 'mcp-server.log').open('a') as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=45)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == 'atelier-routines'
                yield session


async def rpc(session, name, arguments):
    result = await session.call_tool(name, arguments)
    body = json.loads(result.content[0].text) if not result.isError else None
    if body is None:
        try:
            body = json.loads(result.content[0].text)
        except json.JSONDecodeError:
            body = {'status': 'error', 'message': result.content[0].text}
    return body, result


class MCPTest(unittest.TestCase):
    def test_real_stdio(self):
        asyncio.run(self.check_stdio())

    async def check_stdio(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            before = command(project, 'git', 'diff', '--binary')
            staged_before = command(project, 'git', 'diff', '--cached', '--binary')
            original = hashlib.sha256((project / 'src/sample.py').read_bytes()).hexdigest()
            async with connection(project) as session:
                tools = (await session.list_tools()).tools
                self.assertEqual([t.name for t in tools], ['routines_find', 'routines_run'])
                self.assertLess(len(json.dumps([t.model_dump(mode='json') for t in tools])), 2400)
                found, _ = await rpc(session, 'routines_find', {'query': 'chercher code', 'limit': 1})
                self.assertEqual(found['matches'][0]['id'], 'project.search')
                self.assertEqual(found['matches'][0]['availability'], 'executable')
                self.assertEqual(found['matches'][0]['params'], executor.SCHEMAS['project.search'])
                for entry in executor.catalogue():
                    if entry['kind'] == 'proposal':
                        result, _ = await rpc(session, 'routines_find', {'query': entry['id']})
                        self.assertNotIn(entry['id'], [e['id'] for e in result['matches']])
                examples = {
                    'system.context': {}, 'project.manifest': {}, 'project.tree': {},
                    'project.outline': {'file': 'src/sample.py'},
                    'project.search': {'pattern': 'calculate', 'path': 'src'},
                    'project.symbol': {'pattern': 'calculate'},
                    'git.status': {}, 'git.diff': {}, 'git.staged': {},
                    'files.json': {'file': 'data.json'}, 'files.csv': {'file': 'data.csv'},
                    'files.logs': {'file': 'events.log'},
                }
                for identifier, values in examples.items():
                    with self.subTest(routine=identifier):
                        body, result = await rpc(session, 'routines_run', {'id': identifier, 'params': {'project': str(project), **values}})
                        self.assertFalse(result.isError, body)
                        self.assertEqual(body['status'], 'ok', body)
                        self.assertTrue(body['evidence'])
                        receipt = json.loads(Path(body['evidence'][0]).read_text())
                        summary = receipt['result']['summary']
                        self.assertLessEqual(len(result.content[0].text.encode()), 2000)
                        if identifier == 'files.csv':
                            self.assertEqual(summary, {'rows': 2, 'columns': 2, 'empty_cells': 1})
                        if identifier == 'files.json':
                            self.assertEqual(summary['items'], 2)
                        if identifier == 'files.logs':
                            self.assertEqual(summary['matching_lines'], 2)
                            self.assertNotIn('synthetic-value', json.dumps(receipt))
                        if identifier == 'git.status':
                            self.assertEqual(summary['unstaged']['files'], 1)
                            self.assertEqual(summary['staged']['files'], 1)
                            self.assertEqual(body['internal_commands'], 3)
                        if identifier in ('project.search', 'project.symbol'):
                            self.assertEqual(summary['matches'], 1)
                            self.assertNotIn('.env', str(receipt['result']['details']))
                        if identifier == 'project.outline':
                            self.assertEqual(summary['declarations'], 2)
                guided, _ = await rpc(session, 'routines_run', {'id': 'system.vps', 'params': {}})
                self.assertEqual(guided['status'], 'guided')
                self.assertTrue(guided['procedure'])
                proposal = next(e['id'] for e in executor.catalogue() if e['kind'] == 'proposal')
                excluded, result = await rpc(session, 'routines_run', {'id': proposal, 'params': {}})
                self.assertEqual(excluded['status'], 'unavailable')
                self.assertTrue(result.isError)
                (project / 'escape.py').symlink_to(Path(tmp) / 'outside.py')
                (project / 'bad.json').write_text('{broken')
                (project / 'bad.csv').write_text('a,b\n1\n')
                (project / 'bad.py').write_text('def broken(')
                (project / 'large.txt').touch()
                with (project / 'large.txt').open('r+b') as out:
                    out.truncate(executor.MAX_INPUT + 1)
                os.mkfifo(project / 'fifo.txt')
                invalid = [
                    {'id': 'unknown', 'params': {}},
                    {'id': 'files.json', 'params': {}},
                    {'id': 'git.status', 'params': {'project': str(project), 'command': 'echo no'}},
                    {'id': 'git.status', 'params': {'project': '/etc'}},
                    *[{'id': 'files.json', 'params': {'project': str(project), 'file': f}} for f in
                      ('CLAUDE.md', 'sub/CLAUDE.md', '.env', '.git/config', '../outside.json',
                       '/etc/passwd', 'escape.py', 'large.txt', 'fifo.txt', 'bad.json')],
                    {'id': 'files.csv', 'params': {'project': str(project), 'file': 'bad.csv'}},
                    {'id': 'project.outline', 'params': {'project': str(project), 'file': 'bad.py'}},
                    {'id': 'project.search', 'params': {'project': str(project), 'pattern': []}},
                    {'id': 'project.search', 'params': {'project': str(project), 'pattern': 'x', 'path': '.env'}},
                ]
                for arguments in invalid:
                    with self.subTest(invalid=arguments):
                        body, result = await rpc(session, 'routines_run', arguments)
                        self.assertEqual(body['status'], 'error', body)
                        self.assertTrue(result.isError)
                for arguments in ({'query': ''}, {'query': 'git', 'limit': 0}, {'query': 'git', 'limit': True}, {'query': 'git', 'shell': 'x'}):
                    body, result = await rpc(session, 'routines_find', arguments)
                    self.assertTrue(result.isError, body)
                literal = '$(touch never-created);calculate'
                body, _ = await rpc(session, 'routines_run', {'id': 'project.search', 'params': {'project': str(project), 'pattern': literal}})
                self.assertEqual(body['result']['summary']['matches'], 0)
                self.assertFalse((project / 'never-created').exists())
                (project / 'many.json').write_text(json.dumps({f'item_{i}': i for i in range(2000)}))
                large, result = await rpc(session, 'routines_run', {'id': 'files.json', 'params': {'project': str(project), 'file': 'many.json'}})
                self.assertEqual(large['status'], 'ok')
                self.assertTrue(large['response_truncated'])
                proof = json.loads(Path(large['evidence'][0]).read_text())
                self.assertEqual(len(proof['result']['details']), 2000)
                (project / 'many.txt').write_text(('calculate ' + 'x' * 80 + '\n') * 3000)
                partial, result = await rpc(session, 'routines_run', {'id': 'project.search', 'params': {'project': str(project), 'pattern': 'calculate', 'path': 'many.txt'}})
                self.assertEqual(partial['status'], 'partial', partial)
                self.assertLessEqual(len(result.content[0].text.encode()), 2000)
                cli = command(project, sys.executable, str(HERE / 'routines.py'), 'find', 'git.status', '--limit', '1')
                self.assertEqual(json.loads(cli)['matches'][0]['id'], 'git.status')
                guided_cli = subprocess.run([RTK, 'proxy', sys.executable, str(HERE / 'routines.py'), 'run',
                                             'system.vps', '--params', '{}'], text=True, capture_output=True, timeout=30)
                self.assertEqual(guided_cli.returncode, 2)
                self.assertEqual(json.loads(guided_cli.stdout)['status'], 'guided')
            self.assertEqual(before, command(project, 'git', 'diff', '--binary'))
            self.assertEqual(staged_before, command(project, 'git', 'diff', '--cached', '--binary'))
            self.assertEqual(original, hashlib.sha256((project / 'src/sample.py').read_bytes()).hexdigest())

    def test_real_timeout_and_unavailable(self):
        asyncio.run(self.check_failures())

    async def check_failures(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            async with connection(project, ATELIER_ROUTINES_TIMEOUT='0.000001') as session:
                body, result = await rpc(session, 'routines_run', {'id': 'git.status', 'params': {'project': str(project)}})
                self.assertEqual(body['status'], 'timeout', body)
                self.assertTrue(result.isError)
                self.assertEqual(json.loads(Path(body['evidence'][0]).read_text())['status'], 'timeout')
            async with connection(project, PATH=str(project)) as session:
                body, result = await rpc(session, 'routines_run', {'id': 'git.status', 'params': {'project': str(project)}})
                self.assertEqual(body['status'], 'unavailable', body)
                self.assertTrue(result.isError)
            nonrepo = project / 'nonrepo'
            nonrepo.mkdir()
            # A directory outside Git is needed: Git searches parents by default.
            outside = Path(tmp) / 'nonrepo'
            outside.mkdir()
            async with connection(outside) as session:
                body, result = await rpc(session, 'routines_run', {'id': 'git.status', 'params': {'project': str(outside)}})
                self.assertEqual(body['status'], 'error', body)
                self.assertTrue(result.isError)


if __name__ == '__main__':
    unittest.main(verbosity=2)
