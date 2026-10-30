import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import wait_hook
from test_mcp import connection, make_fixture, rpc


class LocalWaitTest(unittest.TestCase):
    def test_scoped_owner_and_pid_identity_are_required(self):
        process = {'pid': 123, 'uid': os.getuid(), 'started': 'known', 'state': 'S'}
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp)
            with patch.object(wait_hook, 'identity', return_value=process), patch.object(wait_hook, 'working_directory', return_value=project):
                self.assertEqual(wait_hook.find_process(project, 123), process)
            with patch.object(wait_hook, 'identity', return_value={**process, 'uid': os.getuid() + 1}):
                with self.assertRaises(wait_hook.MonitorError):
                    wait_hook.find_process(project, 123)
            with patch.object(wait_hook, 'identity', return_value=process), patch.object(wait_hook, 'working_directory', return_value=project.parent):
                with self.assertRaises(wait_hook.MonitorError):
                    wait_hook.find_process(project, 123)
        with patch.object(wait_hook, 'identity', return_value={**process, 'started': 'reused'}):
            self.assertEqual(wait_hook.wait_process(process, 1)['status'], 'decision_required')
        with patch.object(wait_hook, 'identity', return_value=None):
            result = wait_hook.wait_process(process, 1)
            self.assertEqual(result['status'], 'partial')
            self.assertFalse(result['tests_validated'])
        stop = threading.Event()
        stop.set()
        with self.assertRaises(wait_hook.MonitorError):
            wait_hook.wait_process(process, 1, stop)

    def test_unique_canonical_guard_is_detected_and_ambiguity_refused(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp)
            guard = Path(__file__).with_name('guards') / 'workspace-budget.py'
            process = {'pid': 123, 'uid': os.getuid(), 'started': 'known', 'state': 'S'}
            def command(argv, limit=16384):
                if argv[0] == 'pgrep':
                    return 0, '123\n'
                return 0, f'{sys.executable} {guard} --temp-gib 0.001 run . -- python fixture.py'
            with patch.object(wait_hook, 'command', side_effect=command), patch.object(wait_hook, 'identity', return_value=process), patch.object(wait_hook, 'working_directory', return_value=project):
                self.assertEqual(wait_hook.find_process(project), process)
            with patch.object(wait_hook, 'command', return_value=(1, '')):
                with self.assertRaises(wait_hook.MonitorError):
                    wait_hook.find_process(project)

    def test_real_stdio_wait_deadline_resume_and_failed_child(self):
        asyncio.run(self.check_stdio())

    async def check_stdio(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            process = subprocess.Popen(['rtk', 'proxy', sys.executable, '-c', 'import time; time.sleep(6); raise SystemExit(7)'],
                                       cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with connection(project) as session:
                    args = {'id': 'jobs.wait', 'params': {'project': str(project), 'pid': process.pid, 'seconds': 1},
                            'request_id': 'known-local-wait'}
                    result, response = await rpc(session, 'routines_run', args)
                    self.assertFalse(response.isError, result)
                    self.assertEqual(result['status'], 'running', result)
                    reference = result['execution_id']
                    result, _ = await rpc(session, 'routines_run', {'id': 'executions.wait', 'params': {
                        'project': str(project), 'execution_id': reference, 'seconds': 10}})
                    self.assertEqual(result['status'], 'partial', result)
                    self.assertFalse(result['result']['tests_validated'])
                    self.assertEqual(result['validation_status'], 'not_requested')
                    self.assertEqual(process.wait(timeout=3), 7)
                    old, _ = await rpc(session, 'routines_run', args)
                    self.assertEqual(old['execution_id'], reference)
                    self.assertFalse(old['facts_current'])
                    invalid, response = await rpc(session, 'routines_run', {'id': 'jobs.wait', 'params': {
                        'project': str(project), 'pid': -1}})
                    self.assertTrue(response.isError)
                    self.assertEqual(invalid['status'], 'error')
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
