"""Installed compaction hook, local model transport and safe recovery."""
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import decision_payload
import install
import usage


class CompactHookTest(unittest.TestCase):
    def test_installed_hook_selects_before_compact_and_delivers_once(self):
        requests = []

        class D1(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                assert self.path == '/v1/systemone' and self.headers['Authorization'] == 'Bearer fixture'
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                if len(requests) == 3:
                    self.send_error(503)
                    return
                answers = {'receipt_1': {'noul': .0001}, 'receipt_2': {'noul': .5}}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'answers': answers}).encode())

        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as temporary:
            root = Path(temporary).resolve()
            project = root / 'repo'
            cwd = project / 'src'
            cwd.mkdir(parents=True)
            other = root / 'other'
            other.mkdir()
            with patch.dict(os.environ, {'HOME': str(root), 'ATELIER_ROUTINES_STORE': str(root/'store'),
                                         'ATELIER_ROUTINES_USAGE': 'on', 'ATELIER_ROUTINES_DECISION': 'on'}):
                config = root / 'codex'
                config.mkdir()
                (config / 'config.toml').write_text('[mcp_servers.atelier-routines.env]\nATELIER_ROUTINES_ROOTS = '
                    + json.dumps(json.dumps([str(root)])) + '\n')
                original = {'matcher': '.*', 'hooks': [{'type': 'command', 'command': 'other-hook'}]}
                (config / 'hooks.json').write_text(json.dumps({'hooks': {'PreCompact': [original]}}))
                rtk = shutil.which('rtk')
                install.install_hooks(config, sys.executable, rtk)
                first = (config / 'hooks.json').read_bytes()
                install.install_hooks(config, sys.executable, rtk)
                self.assertEqual((config / 'hooks.json').read_bytes(), first)
                hooks = json.loads(first)['hooks']
                self.assertIn(original, hooks['PreCompact'])
                handlers = {event: next(row for row in hooks[event] if row['hooks'][0].get('statusMessage', '').endswith(' d1'))
                            for event in ('PreCompact', 'PostCompact', 'SessionStart')}
                self.assertEqual(handlers['PreCompact']['matcher'], '^(auto|manual)$')
                self.assertEqual(handlers['SessionStart']['matcher'], '^compact$')
                archive = Path(shlex.split(handlers['PreCompact']['hooks'][0]['command'])[4])
                self.assertIn(hashlib.sha256(archive.read_bytes()).hexdigest(), archive.name)
                with zipfile.ZipFile(archive) as bundle:
                    self.assertEqual(set(bundle.namelist()), {'__main__.py', 'usage.py', 'decision_runtime.py', 'decision_payload.py'})

                store = root / 'store-executions'
                store.mkdir(mode=0o700)
                (store / 'history').mkdir(mode=0o700)
                usage.record('hook', 'prompt', 'unknown', session='fixture-chat', prompt='Investigate authentication tests')
                expected = {'uncertain': '1'*32, 'irrelevant': '2'*32, 'failure': '3'*32, 'foreign': '4'*32}
                for key, identifier in expected.items():
                    home = other if key == 'foreign' else project
                    operation = 'checks.regression' if key == 'failure' else 'project.search'
                    status = 'error' if key == 'failure' else 'ok'
                    path = store / ('history' if key == 'uncertain' else '') / (identifier + '.json')
                    path.write_text(json.dumps({'execution_id': identifier, 'project': str(home), 'id': operation,
                        'state': status, 'summary': {'matches': 2, 'secret': 'untrusted-canary'},
                        'validation_status': 'failed' if key == 'failure' else 'not_requested', 'proof_hashes': {'fixture': 'yes'}}))
                    path.chmod(0o600)
                    usage.record('hook', 'end', operation, session='fixture-chat',
                                 result={'status': status, 'execution_id': identifier})

                service = HTTPServer(('127.0.0.1', 0), D1)
                worker = threading.Thread(target=service.serve_forever)
                worker.start()
                endpoint = root / '.local/share/atelier-routines-models/d1/endpoint.json'
                endpoint.parent.mkdir(parents=True, mode=0o700)
                endpoint.write_text(json.dumps({'port': service.server_port, 'token': 'fixture',
                                                'model_sha256': decision_payload.MODEL_SHA}))
                endpoint.chmod(0o600)

                def run(event, **overrides):
                    command = shlex.split(handlers[event]['hooks'][0]['command'])
                    body = {'hook_event_name': event, 'session_id': 'fixture-chat', 'cwd': str(cwd),
                            'trigger': 'manual', 'turn_id': 'turn-1', 'source': 'compact', **overrides}
                    call = subprocess.run(command, input=json.dumps(body), text=True, capture_output=True, timeout=12, check=True)
                    return json.loads(call.stdout) if call.stdout else None

                try:
                    self.assertIsNone(run('PreCompact'))
                    self.assertEqual(len(requests), 1)
                    self.assertIsNone(run('SessionStart'))
                    self.assertIsNone(run('PostCompact', turn_id='other-turn'))
                    self.assertIsNone(run('SessionStart'))
                    self.assertIsNone(run('PostCompact'))
                    self.assertIsNone(run('SessionStart', session_id='other-chat'))
                    self.assertIsNone(run('SessionStart', cwd=str(other)))
                    self.assertIsNone(run('SessionStart', source='resume'))
                    note = run('SessionStart')['hookSpecificOutput']['additionalContext']
                    for kept in ('uncertain', 'failure'):
                        self.assertIn(expected[kept], note)
                    for omitted in ('irrelevant', 'foreign'):
                        self.assertNotIn(expected[omitted], note)
                    self.assertIn('validation=failed', note)
                    self.assertIsNone(run('SessionStart'))

                    for turn in ('turn-2', 'turn-3'):
                        self.assertIsNone(run('PreCompact', trigger='auto', turn_id=turn))
                        self.assertIsNone(run('PostCompact', trigger='auto', turn_id=turn))
                        note = run('SessionStart')['hookSpecificOutput']['additionalContext']
                    self.assertEqual(len(requests), 3)
                    self.assertIn(expected['irrelevant'], note)
                    self.assertNotIn('untrusted-canary', json.dumps(requests))
                    self.assertTrue((store / (expected['irrelevant'] + '.json')).exists())
                    with usage.database(create=False) as database:
                        counts = database.execute('''SELECT status,json_extract(data,'$.delivery'),json_extract(data,'$.d1.accepted')
                            FROM events WHERE source='local-compaction' ORDER BY seq''').fetchall()
                    self.assertEqual(counts, [('partial', 'delivered', 1), ('partial', 'delivered', 1),
                                              ('unavailable', 'delivered', None)])
                finally:
                    service.shutdown()
                    worker.join()
                    service.server_close()
