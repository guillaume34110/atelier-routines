"""Exercise ledger renewal and saturated-store recovery through real MCP stdio."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import executions
from test_mcp import connection, make_fixture, rpc


def seed(store, record, count, padded=False):
    store.mkdir(mode=0o700, exist_ok=True)
    for index in range(count):
        identifier = f'{index:032x}'
        data = json.dumps({**record, 'execution_id': identifier}).encode()
        path = store / (identifier + '.json')
        path.write_bytes(data.ljust(16384, b' ') if padded else data)
        path.chmod(0o600)


class ExecutionHistoryTest(unittest.TestCase):
    def test_real_stdio_legacy_overflow_preserves_history_and_request_identity(self):
        asyncio.run(self.check_renewal())

    async def check_renewal(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            store = project.parent / (project.name + '-definitions-executions')
            async with connection(project) as session:
                args = {'id': 'files.logs', 'params': {'project': str(project), 'file': 'events.log'}}
                first, _ = await rpc(session, 'routines_run', args)
                self.assertEqual(first['status'], 'ok', first)
                name = first['execution_id'] + '.json'
                anchor = json.loads((store / name).read_bytes())
                anchor['created_at'] = -1
                (store / name).write_text(json.dumps(anchor))
                seed(store, {**anchor, 'project': str(project.parent / 'other-project'), 'state': 'error',
                             'signature': '0' * 64, 'request_key': None, 'created_at': 0}, 1024)
                active = store / ('0' * 32 + '.json')
                data = json.loads(active.read_bytes())
                data.update(state='running', owner_pid=os.getpid())
                active.write_text(json.dumps(data))
                before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in store.glob('*.json')}
                inspected, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': first['execution_id']}})
                self.assertEqual(inspected['status'], 'ok', inspected)
                renewed, _ = await rpc(session, 'routines_run', {'id': 'project.manifest', 'params': {'project': str(project)}})
                self.assertEqual(renewed['status'], 'ok', renewed)
                archive = store / 'history'
                self.assertTrue((archive / name).is_file())
                self.assertTrue(active.is_file())
                self.assertEqual(active.read_bytes(), json.dumps(data).encode())
                self.assertLessEqual(len(list(store.glob('*.json'))), executions.MAX_HOT)
                after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in store.rglob('*.json')}
                self.assertEqual(len(after), len(before) + 1)
                self.assertTrue(all(after[k] == value for k, value in before.items()))
                keyed = {**args, 'request_id': 'archived-log-request'}
                reused, _ = await rpc(session, 'routines_run', keyed)
                self.assertEqual(reused['execution_id'], first['execution_id'])
                self.assertTrue(reused['freshness_verified'])
                historical, _ = await rpc(session, 'routines_run', keyed)
                self.assertEqual(historical['execution_id'], first['execution_id'])
                self.assertTrue(historical['historical'])
                self.assertFalse(historical['facts_current'])
                Path(first['evidence'][0]).unlink()
                missing, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': first['execution_id']}})
                self.assertEqual(missing['status'], 'decision_required', missing)

    def test_real_stdio_byte_budget_refuses_creation_but_keeps_inspection(self):
        asyncio.run(self.check_limits(padded=True))

    def test_real_stdio_active_records_are_never_evicted(self):
        asyncio.run(self.check_limits(padded=False))

    async def check_limits(self, padded):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            store = project.parent / (project.name + '-definitions-executions')
            record = {'project': str(project if padded else project.parent / 'other-project'), 'id': 'project.search',
                      'state': 'error' if padded else 'starting', 'signature': '0' * 64,
                      'owner_pid': os.getpid(), 'evidence': [], 'stages': [], 'validation_status': 'not_requested'}
            seed(store, record, 1024, padded=padded)
            async with connection(project) as session:
                rejected, _ = await rpc(session, 'routines_run', {'id': 'git.status', 'params': {'project': str(project)}})
                self.assertEqual(rejected['status'], 'error' if padded else 'decision_required', rejected)
                self.assertIn('16 Mio' if padded else 'actif plein', rejected.get('message', ''))
                self.assertEqual(len(list(store.glob('*.json'))), 1024)
                self.assertEqual(len(list((store / 'history').glob('*.json'))), 0)
                # Lookup checks project scope without scanning or admitting another record.
                if not padded:
                    record['project'] = str(project)
                    path = store / ('0' * 32 + '.json')
                    path.write_bytes(json.dumps({**record, 'execution_id': '0' * 32}).encode())
                inspected, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': '0' * 32}})
                self.assertEqual(inspected['execution_id'], '0' * 32, inspected)
                self.assertEqual(inspected['status'], record['state'])
                self.assertEqual(inspected['validation_status'], 'not_requested')

    def test_real_stdio_history_symlink_is_refused(self):
        asyncio.run(self.check_symlink())

    async def check_symlink(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            store = project.parent / (project.name + '-definitions-executions')
            store.mkdir(mode=0o700)
            target = project.parent / 'untouched'
            target.mkdir()
            (store / 'history').symlink_to(target, target_is_directory=True)
            async with connection(project) as session:
                result, _ = await rpc(session, 'routines_run', {'id': 'git.status', 'params': {'project': str(project)}})
                self.assertEqual(result['status'], 'error', result)
                self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
