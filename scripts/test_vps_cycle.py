import os
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import vps_cycle


class InstalledClientTest(unittest.TestCase):
    def test_failed_suite_is_collected_without_benchmark_after_sources_change(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp)
            temporary = project / 'transfer'
            temporary.mkdir()
            execution_id = 'a' * 32
            record = {'project': str(project), 'remote_job': 'fake:job', 'source': {'old': True}, 'created_at': time.time()}
            result = []
            def unpack(archive, destination):
                files = {'receipt.json': json.dumps({'id': 'job', 'agent': 'routines-' + execution_id[:16],
                         'argv': vps_cycle.COMMAND, 'status': 'failed', 'returncode': 1}),
                         'stdout.log': 'failed', 'work/output/task-tmp/tests.log': 'FAIL: concurrency',
                         'work/output/task-tmp/validation.json': json.dumps({'passed': False, 'complete': False,
                         'tests': 1, 'failures': 1, 'errors': 0})}
                for name, data in files.items():
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(data)
            client = SimpleNamespace(wait_for_job=lambda *a, **k: {'status': 'failed'},
                     job_target=lambda job: ({'ssh_host': 'unused'}, 'job'),
                     transport=lambda *a, **k: (0, b''), unpack_result=unpack,
                     json_request=lambda *a, **k: (0, {'released': 'job'}))
            with patch.dict(os.environ, {'TASK_WORKSPACE': str(temporary)}), \
                 patch.object(vps_cycle, 'client', return_value=client), \
                 patch.object(vps_cycle, 'check_project'), \
                 patch.object(vps_cycle.core, 'project_path', return_value=project), \
                 patch.object(vps_cycle, 'source_signature', return_value={'changed': True}), \
                 patch.object(vps_cycle.executions, 'get', return_value=record), \
                 patch.object(vps_cycle.executions, 'update', side_effect=lambda _, **changes: record.update(changes)), \
                 patch.object(vps_cycle.executions, 'finish', side_effect=lambda _, proof: result.append(proof)), \
                 redirect_stdout(io.StringIO()):
                vps_cycle.bridge(execution_id, 'resume')
            self.assertEqual(result[0]['status'], 'error')
            self.assertEqual(result[0]['validation_status'], 'failed')
            self.assertFalse(result[0]['result']['summary']['source_unchanged'])
            self.assertTrue(record['released'])
            self.assertEqual(len(result[0]['artifacts']), 4)
            self.assertFalse((project / 'output/routines-runs' / execution_id / 'benchmark.json').exists())

    def test_exact_installed_launcher_and_refused_shell_code(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            home = Path(tmp)
            launcher = home / '.local/bin/atelier-vps'
            launcher.parent.mkdir(parents=True)
            script = home / 'client with spaces/vps.py'
            script.parent.mkdir()
            script.write_text('# synthetic client\n')
            with patch.object(Path, 'home', return_value=home):
                self.assertIsNone(vps_cycle.client_path())
                launcher.write_text(f'#!/bin/sh\nexec /usr/bin/python3 "{script}" "$@"\n')
                self.assertEqual(vps_cycle.client_path(), script)
                launcher.write_text(f'#!/bin/sh\nexec /usr/bin/python3 "{script}" "$@"; touch marker\n')
                self.assertIsNone(vps_cycle.client_path())
                launcher.write_text(f'#!/bin/sh\neval "{script}"\n')
                self.assertIsNone(vps_cycle.client_path())
                self.assertFalse((home / 'marker').exists())
