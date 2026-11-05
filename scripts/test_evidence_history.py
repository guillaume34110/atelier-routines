"""Real stdio coverage for bounded receipt rotation and historical references."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import executor
from test_mcp import connection, make_fixture, rpc


def seed(folder, count, status='ok', size=None):
    folder.mkdir(mode=0o700, exist_ok=True)
    paths = []
    for _ in range(count):
        path = folder / (uuid.uuid4().hex + '.json')
        data = json.dumps({'id': 'git.status', 'status': status,
                           'operation_state': 'running' if status == 'running' else 'completed'}).encode()
        path.write_bytes(data.ljust(size, b' ') if size else data)
        path.chmod(0o600)
        paths.append(path)
    return paths


def hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*.json')}


class EvidenceHistoryTest(unittest.TestCase):
    def test_stdio_rotation_preserves_bytes_ids_and_old_references(self):
        asyncio.run(self.check_rotation())

    async def check_rotation(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            async with connection(project) as session:
                args = {'id': 'files.logs', 'params': {'project': str(project), 'file': 'events.log'},
                        'request_id': 'proof-renewal-anchor'}
                first, _ = await rpc(session, 'routines_run', args)
                self.assertEqual(first['status'], 'ok', first)
                original = Path(first['evidence'][0])
                folder = original.parent
                os.utime(original, ns=(1, 1))
                paths = seed(folder, executor.MAX_HOT_EVIDENCE - 1)
                active, uncertain = paths[:2]
                active.write_text(json.dumps({'status': 'running', 'operation_state': 'running'}))
                uncertain.write_text(json.dumps({'status': 'decision_required', 'effect_uncertain': True}))
                before = hashes(folder)
                renewed, _ = await rpc(session, 'routines_run', {
                    'id': 'project.manifest', 'params': {'project': str(project)}})
                self.assertEqual(renewed['status'], 'ok', renewed)
                archived = folder / 'history' / original.name
                self.assertTrue(archived.is_file())
                self.assertTrue(active.is_file() and uncertain.is_file())
                self.assertLess(len(list(folder.glob('*.json'))), executor.MAX_HOT_EVIDENCE)
                after = hashes(folder)
                self.assertEqual(len(after), len(before) + 1)
                self.assertTrue(all(after[name] == digest for name, digest in before.items()))
                inspected, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': first['execution_id']}})
                self.assertEqual(inspected['status'], 'ok', inspected)
                self.assertEqual(inspected['evidence'], [str(archived)])
                self.assertEqual(inspected['validation_status'], 'not_requested')
                keyed, _ = await rpc(session, 'routines_run', args)
                self.assertEqual(keyed['execution_id'], first['execution_id'])
                self.assertTrue(keyed['historical'])
                self.assertEqual(keyed['evidence'], [str(archived)])
                legacy_path, _ = await rpc(session, 'routines_run', {'id': 'files.json', 'params': {
                    'project': str(project), 'file': str(original.relative_to(project))}})
                self.assertEqual(legacy_path['status'], 'ok', legacy_path)
                original_data = archived.read_bytes()
                archived.write_bytes(original_data + b' ')
                tampered, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': first['execution_id']}})
                self.assertEqual(tampered['status'], 'decision_required', tampered)
                archived.write_bytes(original_data)
                archive = folder / 'history'
                archive.rename(folder / 'held-history')
                outside = Path(tmp) / 'outside'
                outside.mkdir()
                (outside / original.name).write_bytes(original_data)
                archive.symlink_to(outside, target_is_directory=True)
                linked, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': first['execution_id']}})
                self.assertEqual(linked['status'], 'decision_required', linked)

    def test_stdio_combined_byte_cap_preserves_history_and_inspection(self):
        asyncio.run(self.check_byte_cap())

    async def check_byte_cap(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            async with connection(project) as session:
                first, _ = await rpc(session, 'routines_run', {'id': 'project.manifest', 'params': {'project': str(project)}})
                self.assertEqual(first['status'], 'ok', first)
                original = Path(first['evidence'][0])
                remaining = executor.MAX_EVIDENCE - executor.MAX_CAPTURE + 1 - original.stat().st_size
                archive = original.parent / 'history'
                while remaining:
                    size = min(remaining, executor.MAX_CAPTURE)
                    seed(archive, 1, size=size)
                    remaining -= size
                before = hashes(original.parent)
                refused, _ = await rpc(session, 'routines_run', {'id': 'git.status', 'params': {'project': str(project)}})
                self.assertEqual(refused['status'], 'decision_required', refused)
                self.assertFalse(refused['evidence'])
                self.assertEqual(hashes(original.parent), before)
                inspected, _ = await rpc(session, 'routines_run', {'id': 'executions.inspect', 'params': {
                    'project': str(project), 'execution_id': first['execution_id']}})
                self.assertEqual(inspected['status'], 'ok', inspected)

    def test_stdio_active_receipts_are_never_archived(self):
        asyncio.run(self.check_active_cap())

    async def check_active_cap(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            folder = project / 'output/routines-evidence'
            folder.mkdir(mode=0o700, parents=True)
            seed(folder, executor.MAX_HOT_EVIDENCE, status='running')
            before = hashes(folder)
            async with connection(project) as session:
                refused, _ = await rpc(session, 'routines_run', {'id': 'project.manifest', 'params': {'project': str(project)}})
                self.assertEqual(refused['status'], 'decision_required', refused)
                self.assertFalse(refused['evidence'])
                self.assertEqual(hashes(folder), before)
                self.assertFalse(any((folder / 'history').iterdir()))

    def test_stdio_symbolic_archive_is_refused(self):
        asyncio.run(self.check_symbolic_archive())

    async def check_symbolic_archive(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            async with connection(project) as session:
                first, _ = await rpc(session, 'routines_run', {'id': 'project.manifest', 'params': {'project': str(project)}})
                self.assertEqual(first['status'], 'ok', first)
                folder = Path(first['evidence'][0]).parent
                (folder / 'history').symlink_to(outside, target_is_directory=True)
                refused, _ = await rpc(session, 'routines_run', {'id': 'git.status', 'params': {'project': str(project)}})
                self.assertEqual(refused['status'], 'error', refused)
                self.assertFalse(refused['evidence'])
                self.assertFalse(any(outside.iterdir()))

    def test_combined_record_cap_counts_archived_receipts(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp) / 'project'
            folder = project / 'output/routines-evidence'
            folder.mkdir(mode=0o700, parents=True)
            seed(folder, 1)
            seed(folder / 'history', 1)
            before = hashes(folder)
            with patch.object(executor, 'MAX_EVIDENCE_RECORDS', 2):
                with self.assertRaises(executor.Refusal):
                    with executor.evidence_dir(project):
                        self.fail('Combined record cap was ignored')
            self.assertEqual(hashes(folder), before)


if __name__ == '__main__':
    unittest.main()
