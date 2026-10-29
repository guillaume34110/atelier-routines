import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest

from test_mcp import connection, make_fixture, rpc


class ExecutionTest(unittest.TestCase):
    def test_reuse_comparison_and_missing_proofs(self):
        asyncio.run(self.check_reports())

    async def check_reports(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            log = project / 'events.log'
            log.write_text('ERROR same\nERROR resolved\n')
            async with connection(project) as session:
                args = {'id': 'files.logs', 'params': {'project': str(project), 'file': 'events.log'}}
                before, _ = await rpc(session, 'routines_run', args)
                self.assertEqual(before['status'], 'ok', before)
                reused, _ = await rpc(session, 'routines_run', args)
                self.assertTrue(reused['reused'])
                self.assertTrue(reused['freshness_verified'])
                self.assertEqual(before['execution_id'], reused['execution_id'])
                keyed, _ = await rpc(session, 'routines_run', {**args, 'request_id': 'same-log-request'})
                historical, _ = await rpc(session, 'routines_run', {**args, 'request_id': 'same-log-request'})
                self.assertEqual(keyed['execution_id'], historical['execution_id'])
                self.assertTrue(historical['historical'])
                self.assertFalse(historical['facts_current'])
                log.write_text('ERROR same\nERROR new\n')
                after, _ = await rpc(session, 'routines_run', args)
                self.assertNotEqual(before['execution_id'], after['execution_id'])
                compared, _ = await rpc(session, 'routines_run', {'id': 'reports.compare', 'params': {
                    'project': str(project), 'reference': before['execution_id'], 'current': after['execution_id']}})
                self.assertEqual(compared['status'], 'ok', compared)
                self.assertEqual(compared['result']['summary'], {'new': 1, 'persistent': 1, 'resolved': 1, 'explained': False})
                detail, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': after['execution_id'], 'detail': 'errors', 'limit': 1}})
                self.assertEqual(len(detail['result']['entries']), 1)
                self.assertEqual(detail['result']['total_entries'], 2)
                (project / 'report.json').write_text('{"passed":true,"tests":3}')
                assertion = {'id': 'reports.validate', 'params': {'project': str(project), 'file': 'report.json',
                             'assertions': [{'pointer': '/passed', 'equals': True}, {'pointer': '/tests', 'equals': 3}]}}
                valid, _ = await rpc(session, 'routines_run', assertion)
                self.assertEqual(valid['status'], 'ok', valid)
                self.assertEqual(valid['validation_status'], 'passed')
                repeated, _ = await rpc(session, 'routines_run', assertion)
                self.assertNotEqual(valid['execution_id'], repeated['execution_id'])
                (project / 'report.json').write_text('{"passed":false,"tests":3}')
                failed, response = await rpc(session, 'routines_run', assertion)
                self.assertEqual(failed['status'], 'error', failed)
                self.assertEqual(failed['validation_status'], 'failed')
                self.assertTrue(response.isError)
                fresh, _ = await rpc(session, 'routines_run', args)
                Path(fresh['evidence'][0]).unlink()
                missing, _ = await rpc(session, 'routines_run', args)
                self.assertEqual(missing['status'], 'decision_required', missing)
                self.assertNotIn('reused', missing)
                inspected, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': fresh['execution_id']}})
                self.assertEqual(inspected['status'], 'decision_required')
                store = project.parent / (project.name + '-definitions-executions')
                metadata = store / (before['execution_id'] + '.json')
                data = json.loads(metadata.read_text())
                data['engine'] = '0' * 64
                metadata.write_text(json.dumps(data))
                stale, _ = await rpc(session, 'routines_run', {'id': 'reports.compare', 'params': {
                    'project': str(project), 'reference': before['execution_id'], 'current': after['execution_id']}})
                self.assertEqual(stale['status'], 'decision_required', stale)
                no_adapter, _ = await rpc(session, 'routines_run', {'id': 'checks.regression', 'params': {'project': str(project)}})
                self.assertEqual(no_adapter['status'], 'unavailable')

    def test_active_duplicates_and_real_interruption(self):
        asyncio.run(self.check_interruption())

    async def check_interruption(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        entered = threading.Event()
        calls = []

        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                entered.set()
                time.sleep(1.8)
                try:
                    self.send_response(200)
                    self.end_headers()
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
                project = make_fixture(tmp)
                args = {'id': 'web.api-health', 'params': {'project': str(project), 'port': server.server_port}}
                async with connection(project) as primary:
                    request = asyncio.create_task(rpc(primary, 'routines_run', args))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    async with connection(project) as observer:
                        duplicate, _ = await rpc(observer, 'routines_run', args)
                        self.assertEqual(duplicate['status'], 'running', duplicate)
                        identifier = duplicate['execution_id']
                        metadata = project.parent / (project.name + '-definitions-executions') / (identifier + '.json')
                        owner = json.loads(metadata.read_text())['owner_pid']
                        self.assertNotEqual(owner, os.getpid())
                        os.kill(owner, signal.SIGTERM)
                        with self.assertRaises(Exception):
                            await request
                        interrupted, _ = await rpc(observer, 'routines_run', {'id': 'executions.inspect', 'params': {
                            'project': str(project), 'execution_id': identifier}})
                        self.assertEqual(interrupted['status'], 'decision_required', interrupted)
                        again, _ = await rpc(observer, 'routines_run', args)
                        self.assertEqual(again['execution_id'], identifier)
                        self.assertEqual(again['status'], 'decision_required')
                        self.assertEqual(len(calls), 1)
                        refused, _ = await rpc(observer, 'routines_run', {'id': 'executions.resume', 'params': {
                            'project': str(project), 'execution_id': identifier}})
                        self.assertEqual(refused['status'], 'decision_required')
                        resolved, _ = await rpc(observer, 'routines_run', {'id': 'executions.resolve', 'params': {
                            'project': str(project), 'execution_id': identifier,
                            'reason': 'Inspection interrompue constatée ; aucune validation acquise.'}})
                        self.assertEqual(resolved['status'], 'abandoned', resolved)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
