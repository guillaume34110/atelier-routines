import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import routines
import usage
from test_mcp import connection, rpc


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE'])
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'ATELIER_ROUTINES_STORE': str(self.project / 'definitions'),
                                                  'ATELIER_ROUTINES_USAGE': 'on', 'CODEX_HOME': str(self.project / 'codex'),
                                                  'CODEX_THREAD_ID': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def events(self):
        with usage.database() as database:
            return database.execute('SELECT phase,turn,operation,status,data FROM events ORDER BY seq').fetchall()

    def test_real_standalone_hook_keeps_intent_but_not_payloads(self):
        script = Path(usage.__file__)
        def send(event, **values):
            data = {'hook_event_name': event, 'session_id': 'private-session', 'turn_id': 'private-turn', 'cwd': str(self.project), **values}
            result = subprocess.run(['rtk', 'proxy', sys.executable, str(script), '--hook', json.dumps([str(self.project)])],
                                    input=json.dumps(data), text=True, capture_output=True, timeout=5, check=True)
            self.assertEqual((result.stdout, result.stderr), ('', ''))
        send('UserPromptSubmit', prompt='Diagnostiquer le test token=canary-credential password="hidden phrase" /private/file')
        args = {'id': 'files.logs', 'params': {'project': str(self.project), 'file': 'secret-file', 'token': 'hidden-input'}}
        send('PreToolUse', tool_name='mcp__atelier_routines__routines_run', tool_use_id='call-1', tool_input=args)
        send('PostToolUse', tool_name='mcp__atelier_routines__routines_run', tool_use_id='call-1', tool_input=args,
             tool_response={'content': [{'type': 'text', 'text': json.dumps({'status': 'partial', 'validation_status': 'not_requested',
                                                                          'message': 'hidden-output', 'execution_id': 'a' * 32})}]})
        rows = self.events()
        self.assertEqual([r[0] for r in rows], ['prompt', 'start', 'end'])
        self.assertEqual(len({r[1] for r in rows}), 1)
        self.assertEqual(rows[-1][2:4], ('files.logs', 'partial'))
        self.assertIn('Diagnostiquer le test', rows[0][4])
        data = (usage.root() / 'events.sqlite3').read_bytes()
        for forbidden in (b'canary-credential', b'hidden phrase', b'/private/file', b'hidden-input', b'hidden-output', b'private-session'):
            self.assertNotIn(forbidden, data)
        self.assertEqual((usage.root().stat().st_mode & 0o777), 0o700)
        self.assertEqual(((usage.root() / 'events.sqlite3').stat().st_mode & 0o777), 0o600)

    def test_late_result_uses_original_turn_and_duplicate_is_ignored(self):
        usage.record('hook', 'prompt', 'unknown', session='s', prompt='première')
        usage.record('hook', 'start', 'git.status', session='s', call_id='same', inputs={})
        first = self.events()[0][1]
        usage.record('hook', 'prompt', 'unknown', session='s', prompt='suivante')
        for _ in range(2):
            usage.record('hook', 'end', 'git.status', session='s', call_id='same', inputs={}, result={'status': 'ok'})
        self.assertEqual(len(self.events()), 4)
        self.assertEqual(self.events()[-1][1], first)
        self.assertEqual(usage.report()['open_or_missing_results'], 0)

    def test_real_mcp_records_search_and_refusal_before_execution(self):
        async def run():
            async with connection(self.project, ATELIER_ROUTINES_STORE=os.environ['ATELIER_ROUTINES_STORE']) as session:
                await rpc(session, 'routines_find', {'query': 'git.status', 'limit': 1})
                body, _ = await rpc(session, 'routines_run', {'id': 'missing', 'params': {'project': str(self.project)}})
                self.assertEqual(body['status'], 'error')
        asyncio.run(run())
        rows = self.events()
        self.assertEqual([r[0] for r in rows], ['start', 'end', 'start', 'end'])
        self.assertEqual(rows[-1][3], 'error')

    def test_cli_records_and_report_does_not_record_itself(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(routines.main(['find', 'git.status']), 0)
        self.assertEqual(len(self.events()), 2)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(routines.main(['usage']), 0)
        self.assertEqual(json.loads(out.getvalue())['events'], 2)
        self.assertEqual(len(self.events()), 2)

    def test_incomplete_capture_and_bounds_are_explicit(self):
        usage.record('hook', 'prompt', 'unknown', session='s', prompt='observer')
        for i in range(3):
            usage.record('hook', 'end', 'git.status', session='s', inputs={})
        usage.record('hook', 'start', 'jobs.wait', session='s', call_id='pending')
        with patch.object(usage, 'ANALYSIS_LIMIT', 2):
            result = usage.report()
        self.assertTrue(result['analysis_truncated'])
        self.assertEqual(result['open_or_missing_results'], 1)
        self.assertEqual(result['task_completion'], 'not_observed')
        self.assertEqual(result['tokens']['state'], 'unavailable')
        self.assertEqual(result['repeat_inputs_in_turn'], 1)

    def test_quota_rotates_oldest_records_and_capture_errors_do_not_mask_results(self):
        usage.record('cli', 'end', 'first', result={'status': 'ok'})
        errors = io.StringIO()
        with patch.object(usage, 'CAP_BYTES', 65536), redirect_stderr(errors):
            for i in range(500):
                usage.record('cli', 'end', 'other', prompt='x' * 280, inputs={'i': i})
            usage.record('cli', 'end', 'latest', result={'status': 'ok'})
        self.assertEqual(errors.getvalue(), '')
        self.assertEqual(self.events()[-1][2], 'latest')
        self.assertNotIn('first', [row[2] for row in self.events()])
        self.assertTrue(usage.report()['history_rotated'])
        self.assertLessEqual((usage.root() / 'events.sqlite3').stat().st_size, 65536)
        with patch.object(usage, 'database', side_effect=OSError('canary')), redirect_stderr(io.StringIO()):
            with usage.capture('cli', 'test', {}) as observed:
                observed['status'] = 'ok'
            with self.assertRaisesRegex(RuntimeError, 'original'):
                with usage.capture('cli', 'test', {}):
                    raise RuntimeError('original')

    def test_symbolic_and_foreign_stores_are_preserved(self):
        target = self.project / 'existing'
        target.mkdir()
        usage.root().symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            with usage.database():
                pass
        usage.root().unlink()
        usage.root().mkdir(mode=0o700)
        path = usage.root() / 'events.sqlite3'
        with sqlite3.connect(path) as database:
            database.execute('CREATE TABLE personal (value TEXT)')
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'Foreign'):
            with usage.database():
                pass
        with sqlite3.connect(path) as database:
            self.assertEqual(database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('personal',)])

    def test_parallel_writers_and_disable(self):
        usage.record('cli', 'end', 'initial')
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda i: usage.record('cli', 'end', 'parallel', inputs={'i': i}), range(12)))
        self.assertEqual(len(self.events()), 13)
        with patch.dict(os.environ, {'ATELIER_ROUTINES_USAGE': 'off'}):
            usage.record('cli', 'end', 'disabled')
        self.assertEqual(len(self.events()), 13)

    def test_short_writer_contention_preserves_event(self):
        import threading
        import time
        usage.record('cli','end','initial')
        started = threading.Event()
        def write():
            started.set()
            usage.record('cli','end','contended')
        with usage.database() as database, ThreadPoolExecutor(max_workers=1) as pool:
            database.execute('BEGIN IMMEDIATE')
            pending = pool.submit(write)
            try:
                self.assertTrue(started.wait(1))
                time.sleep(1.25)
            finally:
                database.rollback()
            pending.result(timeout=3)
        self.assertEqual([e[2] for e in self.events()],['initial','contended'])

    def test_successful_system_exit_is_not_an_error(self):
        with self.assertRaises(SystemExit):
            with usage.capture('cli', 'version', {}):
                raise SystemExit(0)
        self.assertEqual(self.events()[-1][3], 'ok')

    def test_existing_unversioned_store_keeps_events_and_salt(self):
        usage.record('cli', 'end', 'existing')
        with usage.database() as database, database:
            salt = database.execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
            database.execute("DELETE FROM meta WHERE key='schema_version'")
        self.assertEqual(usage.report()['events'], 1)
        usage.record('cli', 'end', 'new')
        with usage.database() as database:
            self.assertEqual(database.execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0], salt)
        self.assertEqual([row[2] for row in self.events()], ['existing', 'new'])
