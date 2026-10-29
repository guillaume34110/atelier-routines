import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import install


class HookTest(unittest.TestCase):
    def test_real_hook_is_bounded_and_does_not_launch_actions(self):
        script = Path(__file__).with_name('codex_hook.py')
        def invoke(data):
            out = subprocess.run(['rtk', 'proxy', sys.executable, str(script)], input=json.dumps(data),
                                 text=True, capture_output=True, timeout=5, check=True)
            self.assertLess(len(out.stdout.encode()), 1100)
            self.assertEqual(out.stderr, '')
            return json.loads(out.stdout) if out.stdout else None
        self.assertIn('routines_run', invoke({'hook_event_name': 'SessionStart'})['hookSpecificOutput']['additionalContext'])
        for status in ('error', 'partial', 'timeout', 'decision_required', 'running'):
            data = {'hook_event_name': 'PostToolUse', 'tool_name': 'mcp__atelier_routines__routines_run',
                    'tool_response': {'content': [{'type': 'text', 'text': json.dumps({'status': status})}]}}
            self.assertIn('ne valide', invoke(data)['hookSpecificOutput']['additionalContext'])
        data['tool_response'] = {'status': 'ok'}
        self.assertIsNone(invoke(data))
        data['tool_response']['facts_current'] = False
        self.assertIn('historique', invoke(data)['hookSpecificOutput']['additionalContext'])
        data['tool_name'] = 'mcp__other__routines_run'
        self.assertIsNone(invoke(data))
        self.assertIsNone(invoke({'hook_event_name': 'SessionStart', 'oversize': 'x' * 65536}))

    def test_install_preserves_other_hooks_and_trust(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            directory = Path(tmp)
            original = {'description': 'preserve', 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'existing'}]}]}}
            config = directory / 'hooks.json'
            config.write_text(json.dumps(original))
            trust = directory / '.hook-trust'
            trust.write_text('unchanged')
            install.install_hooks(directory, sys.executable, '/rtk')
            first = config.read_bytes()
            installed = json.loads(first)
            self.assertEqual(installed['hooks']['Stop'], original['hooks']['Stop'])
            self.assertEqual(installed['description'], 'preserve')
            self.assertEqual(len(installed['hooks']['SessionStart']), 1)
            self.assertEqual(len(installed['hooks']['PostToolUse']), 1)
            scripts = list((directory / 'hooks').glob('atelier-routines-*.py'))
            self.assertEqual(len(scripts), 1)
            self.assertEqual(scripts[0].read_bytes(), Path(__file__).with_name('codex_hook.py').read_bytes())
            install.install_hooks(directory, sys.executable, '/rtk')
            self.assertEqual(config.read_bytes(), first)
            self.assertEqual(trust.read_text(), 'unchanged')
            scripts[0].write_text('foreign edit')
            with self.assertRaises(ValueError):
                install.install_hooks(directory, sys.executable, '/rtk')
