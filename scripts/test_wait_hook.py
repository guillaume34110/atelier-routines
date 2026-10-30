import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import wait_hook


class WaitHookTest(unittest.TestCase):
    def pre(self, name, args):
        return wait_hook.response({'hook_event_name': 'PreToolUse', 'tool_name': name, 'tool_input': args})

    def test_waits_are_extended_without_changing_the_reference_or_sending_input(self):
        for name in ('write_stdin', 'functions.write_stdin'):
            args = {'session_id': 55991, 'chars': '', 'yield_time_ms': 50000, 'max_output_tokens': 150}
            self.assertIsNone(self.pre(name, args))  # Codex exempts this transport from PreToolUse.
            for chars in ('\x03', 'answer\n'):
                self.assertIsNone(self.pre(name, {**args, 'chars': chars}))
        args = {'cell_id': 'g4:1755', 'yield_time_ms': 20000, 'max_tokens': 150}
        self.assertEqual(self.pre('wait', args)['updatedInput'], {**args, 'yield_time_ms': 900000})
        self.assertIsNone(self.pre('wait', {**args, 'terminate': True}))

    def test_native_log_poll_waits_in_the_hook_without_returning_to_the_model(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp)
            data = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': str(project),
                    'tool_input': {'command': 'rtk proxy tail -n 1 output/task-tmp/stdout.log'}}
            process = {'pid': 123, 'uid': os.getuid(), 'started': 'known', 'state': 'S'}
            with patch.object(wait_hook, 'find_process', return_value=process), patch.object(wait_hook, 'wait_process') as wait:
                wait.return_value = {'status': 'partial', 'tests_validated': False}
                self.assertIn('restent à vérifier', wait_hook.response(data, [project])['additionalContext'])
                wait.assert_called_once_with(process, 900)
                wait.return_value = {'status': 'running', 'tests_validated': False}
                self.assertEqual(wait_hook.response(data, [project])['permissionDecision'], 'deny')
                wait.reset_mock()
                self.assertIsNone(wait_hook.response(data, [project / 'outside']))
                data['tool_input']['command'] = 'rtk proxy sed -n 1p scripts/source.py'
                self.assertIsNone(wait_hook.response(data, [project]))
                wait.assert_not_called()

    def test_only_existing_read_only_waits_are_rewritten(self):
        args = {'job_id': 'server:known-job', 'server': 'default', 'timeout': 60}
        self.assertEqual(self.pre('mcp__atelier_vps__vps_wait', args)['updatedInput'], {**args, 'timeout': 3600})
        args = {'id': 'executions.wait', 'params': {'project': '/scope', 'execution_id': 'a' * 32, 'seconds': 1},
                'request_id': 'unchanged'}
        out = self.pre('mcp__atelier_routines__routines_run', args)['updatedInput']
        self.assertEqual(out, {**args, 'params': {**args['params'], 'seconds': 3600}})
        for identifier in ('checks.regression', 'executions.resume', 'routines.register'):
            self.assertIsNone(self.pre('mcp__atelier_routines__routines_run', {**args, 'id': identifier}))
        for seconds in (0, -1, True, 0.5, 'invalid'):
            self.assertIsNone(self.pre('mcp__atelier_routines__routines_run',
                                        {**args, 'params': {**args['params'], 'seconds': seconds}}))
        for name, values in [('exec_command', {'cmd': 'rtk proxy true'}), ('sleep', {'duration_ms': 10}),
                             ('mcp__other__vps_wait', {'job_id': 'x'}), ('wait', {'cell_id': ''}),
                             ('write_stdin', {'session_id': -1}), ('write_stdin', {'session_id': True}),
                             ('write_stdin', {'session_id': 1, 'yield_time_ms': 'invalid'}),
                             ('write_stdin', {'session_id': 1, 'yield_time_ms': 0.5}),
                             ('write_stdin', {'session_id': 1, 'yield_time_ms': -1})]:
            self.assertIsNone(self.pre(name, values))

    def test_running_results_request_waits_and_do_not_turn_failures_into_success(self):
        data = {'hook_event_name': 'PostToolUse', 'tool_name': 'exec_command', 'tool_input': {},
                'tool_response': {'session_id': 55991, 'output': 'progress'}}
        self.assertIn('55991', wait_hook.response(data)['additionalContext'])
        self.assertIn('900000', wait_hook.response(data)['additionalContext'])
        data['tool_response'] = 'Process running with session ID 55991'
        self.assertIn('55991', wait_hook.response(data)['additionalContext'])
        for code in (0, 7):
            data['tool_response'] = {'exit_code': code, 'output': 'Process running with session ID 55991'}
            self.assertIsNone(wait_hook.response(data))
        data.update(tool_name='mcp__atelier_routines__routines_run', tool_response={
            'content': [{'type': 'text', 'text': json.dumps({'status': 'running', 'execution_id': 'a' * 32})}]})
        self.assertIn('executions.wait', wait_hook.response(data)['additionalContext'])
        data['tool_input'] = {'id': 'executions.wait'}
        self.assertIn('Échéance', wait_hook.response(data)['additionalContext'])
        for status in ('error', 'timeout', 'partial', 'decision_required', 'ok'):
            data['tool_response'] = {'structuredContent': {'status': status, 'execution_id': 'a' * 32}}
            self.assertIsNone(wait_hook.response(data))

    def test_real_hook_protocol_is_bounded_and_does_not_execute_payloads(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            marker = Path(tmp) / 'never-created'
            payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'wait',
                       'tool_input': {'cell_id': 'known-cell', 'yield_time_ms': 20000}}
            script = Path(__file__).with_name('wait_hook.py')
            def invoke(text):
                out = subprocess.run(['rtk', 'proxy', sys.executable, str(script)], input=text,
                                     text=True, capture_output=True, check=True, timeout=5)
                self.assertEqual(out.stderr, '')
                self.assertLess(len(out.stdout.encode()), 1600)
                return json.loads(out.stdout) if out.stdout else None
            self.assertEqual(invoke(json.dumps(payload))['hookSpecificOutput']['updatedInput']['yield_time_ms'], 900000)
            payload['tool_name'] = 'exec_command'
            payload['tool_input'] = {'cmd': f'; touch {marker}'}
            self.assertIsNone(invoke(json.dumps(payload)))
            self.assertFalse(marker.exists())
            for raw in ('not json', '[]', json.dumps({'padding': 'x' * 65536})):
                self.assertIsNone(invoke(raw))
