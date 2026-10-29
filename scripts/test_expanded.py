"""Exercise the additional routines and filesystem boundaries with real stdio calls."""
import asyncio
from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave
import zipfile

import executor
from test_mcp import command, connection, make_fixture, rpc


@contextmanager
def api():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == '/health' else 302 if self.path == '/healthz' else 503)
            self.send_header('Location', 'http://example.invalid/never-follow')
            self.end_headers()
            self.wfile.write(b'private health body omitted')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


class ExpandedTest(unittest.TestCase):
    def test_additional_real_routines(self):
        asyncio.run(self.check_routines())

    async def check_routines(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp, api() as port:
            project = make_fixture(tmp)
            (project / 'AGENTS.md').write_text('Fixture instructions only.\n')
            (project / 'failure.log').write_text('running\nAssertionError: expected 2, got 1\nCaused by: timeout\n')
            (project / 'coverage.json').write_text(json.dumps({'totals': {'covered_lines': 3, 'num_statements': 4}}))
            (project / 'istanbul.json').write_text(json.dumps({'total': {'lines': {'covered': 5, 'total': 10}}}))
            with wave.open(str(project / 'clip.wav'), 'wb') as media:
                media.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                media.writeframes(b'\0\0' * 8000)
            with zipfile.ZipFile(project / 'safe.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('sub/sample.txt', b'x' * 2000000)
            command(project, 'git', 'remote', 'add', 'origin', 'https://sample-user:synthetic-password@example.invalid/team/repo.git?key=synthetic-query')
            examples = {
                'project.instructions': {}, 'project.errors': {'file': 'failure.log'},
                'checks.failure': {'file': 'failure.log'}, 'checks.coverage': {'file': 'coverage.json'},
                'git.log': {}, 'git.blame': {'file': 'src/sample.py', 'line': 1, 'count': 1},
                'git.branches': {}, 'git.remote': {}, 'files.archive': {'file': 'safe.zip'},
                'audio.inspect': {'file': 'clip.wav'}, 'system.preflight': {}, 'system.memory': {},
                'system.storage': {'gib': .001}, 'system.processes': {'pid': os.getpid()},
                'web.ports': {'port': port}, 'web.api-health': {'port': port},
            }
            results = []
            async with connection(project) as session:
                for identifier, params in examples.items():
                    with self.subTest(routine=identifier):
                        body, response = await rpc(session, 'routines_run', {'id': identifier, 'params': {'project': str(project), **params}})
                        entry = next(e for e in executor.catalogue() if e['id'] == identifier)
                        available, _ = executor.availability(entry)
                        results.append({'id': identifier, 'status': body['status'], 'executed': body['status'] == 'ok'})
                        if available == 'unavailable':
                            self.assertEqual(body['status'], 'unavailable', body)
                            self.assertTrue(response.isError)
                            continue
                        self.assertEqual(body['status'], 'ok', body)
                        self.assertFalse(response.isError)
                        proof = json.loads(Path(body['evidence'][0]).read_text())
                        summary = proof['result']['summary']
                        if identifier == 'git.remote':
                            self.assertNotIn('synthetic-password', json.dumps(proof))
                            self.assertNotIn('synthetic-query', json.dumps(proof))
                            self.assertEqual(proof['result']['details'][0]['repository'], 'team/repo.git')
                        if identifier == 'git.blame':
                            self.assertEqual(summary['lines'], 1)
                        if identifier == 'checks.coverage':
                            self.assertEqual(summary['percent'], 75)
                            self.assertFalse(summary['checks_executed'])
                        if identifier == 'project.instructions':
                            self.assertEqual(summary['instruction_files'], 1)
                        if identifier == 'audio.inspect':
                            self.assertEqual(summary['seconds'], 1)
                            self.assertFalse(summary['listening_validated'])
                        if identifier == 'files.archive':
                            self.assertEqual(summary['declared_bytes'], 2000000)
                            self.assertFalse(summary['extraction_performed'])
                            self.assertFalse((project / 'sub/sample.txt').exists())
                        if identifier == 'web.ports':
                            self.assertGreaterEqual(summary['listeners'], 1)
                        if identifier == 'web.api-health':
                            self.assertNotIn('private health body', json.dumps(proof))
                            self.assertFalse(summary['checks_executed'])
                for identifier, params in (
                    ('git.blame', {'file': 'src/sample.py', 'line': True}),
                    ('git.blame', {'file': 'src/sample.py', 'line': 1000}),
                    ('git.log', {'limit': 1000}), ('system.processes', {'pid': '1; echo unsafe'}),
                    ('system.storage', {'gib': -1}), ('system.storage', {'gib': 100001}),
                    ('web.api-health', {'port': port, 'path': 'http://example.invalid'}),
                    ('web.api-health', {'port': port, 'path': '/healthz'}),
                    ('web.api-health', {'port': port, 'path': '/readyz'}),
                ):
                    body, response = await rpc(session, 'routines_run', {'id': identifier, 'params': {'project': str(project), **params}})
                    self.assertEqual(body['status'], 'error', body)
                    self.assertTrue(response.isError)
                for name in ('evil.zip', 'link.zip'):
                    with zipfile.ZipFile(project / name, 'w') as archive:
                        if name == 'evil.zip':
                            archive.writestr('../../escape.txt', 'blocked')
                        else:
                            info = zipfile.ZipInfo('link')
                            info.create_system = 3
                            info.external_attr = (stat.S_IFLNK | 0o777) << 16
                            archive.writestr(info, '/etc/passwd')
                    body, response = await rpc(session, 'routines_run', {'id': 'files.archive', 'params': {'project': str(project), 'file': name}})
                    self.assertEqual(body['status'], 'error', body)
                self.assertFalse((Path(tmp).parent / 'escape.txt').exists())
                body, _ = await rpc(session, 'routines_run', {'id': 'checks.coverage', 'params': {'project': str(project), 'file': 'istanbul.json'}})
                self.assertEqual(body['result']['summary']['percent'], 50)
            (Path(os.environ['TASK_WORKSPACE']) / 'additional-capabilities.json').write_text(json.dumps(results, indent=2))

    def test_read_refuses_parent_replaced_by_link(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp) / 'project'
            outside = Path(tmp) / 'outside'
            (project / 'src').mkdir(parents=True)
            outside.mkdir()
            (project / 'src/data.json').write_text('{"safe":true}')
            (outside / 'data.json').write_text('{"outside":true}')
            original = executor.local_path

            def replace_parent(*args):
                checked = original(*args)
                (project / 'src').rename(project / 'src-held')
                (project / 'src').symlink_to(outside, target_is_directory=True)
                return checked

            with patch.object(executor, 'local_path', side_effect=replace_parent):
                with self.assertRaises(OSError):
                    executor.read_file(project, 'src/data.json')

    def test_evidence_lock_and_links(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp) / 'project'
            project.mkdir()
            with executor.evidence_dir(project) as (path, directory):
                self.assertEqual(os.fstat(directory).st_mode & 0o777, 0o700)
                with self.assertRaises(executor.Refusal):
                    with executor.evidence_dir(project):
                        self.fail('Concurrent evidence accepted')
            (path / '.lock').unlink()
            (path / '.lock').symlink_to(project / 'elsewhere')
            with self.assertRaises(OSError):
                with executor.evidence_dir(project):
                    self.fail('Symbolic lock accepted')

    def test_capture_does_not_forward_credentials(self):
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'synthetic-only', 'PYTHONPATH': '/untrusted'}):
            original = executor.subprocess.Popen
            seen = {}

            def inspect(*args, **kwargs):
                seen.update(kwargs['env'])
                return original(*args, **kwargs)

            with patch.object(executor.subprocess, 'Popen', side_effect=inspect):
                executor.capture(['rtk', 'proxy', 'true'], Path.cwd())
            self.assertNotIn('GITHUB_TOKEN', seen)
            self.assertNotIn('PYTHONPATH', seen)

    def test_search_does_not_follow_ignore_links_or_hard_links(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp) / 'project'
            project.mkdir()
            outside = Path(tmp) / 'outside.txt'
            outside.write_text('needle must not be read\n')
            (project / '.rgignore').symlink_to(outside)
            os.link(outside, project / 'hard.txt')
            (project / 'safe.txt').write_text('needle allowed\n')
            with patch.dict(os.environ, {'ATELIER_ROUTINES_ROOTS': json.dumps([str(project)])}):
                result = executor.execute('project.search', {'project': str(project), 'pattern': 'needle', 'path': '.'})
            self.assertEqual(result['summary']['matches'], 1)
            self.assertNotIn('must not be read', json.dumps(result))
