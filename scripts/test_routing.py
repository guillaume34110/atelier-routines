import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import routing_hook
from test_mcp import connection, make_fixture, rpc


class RoutingTest(unittest.TestCase):
    def invoke(self, data, roots):
        result = subprocess.run(['rtk', 'proxy', sys.executable, str(Path(__file__).with_name('routing_hook.py')),
                                 json.dumps([str(p) for p in roots])], input=json.dumps(data),
                                text=True, capture_output=True, check=True, timeout=5)
        self.assertEqual(result.stderr, '')
        self.assertLess(len(result.stdout.encode()), 2000)
        return json.loads(result.stdout)['hookSpecificOutput'] if result.stdout else None

    def test_prompts_and_subagents_remind_without_starting_actions(self):
        for event in ('UserPromptSubmit', 'SubagentStart'):
            result = self.invoke({'hook_event_name': event, 'prompt': 'Une simple question ?'}, [])
            self.assertEqual(result['hookEventName'], event)
            self.assertIn('routines_run', result['additionalContext'])
            self.assertIn('Une simple question ne lance rien', result['additionalContext'])
            self.assertNotIn('permissionDecision', result)

    def test_shell_routes_and_exceptions_do_not_execute_the_command(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            root = Path(tmp)
            data = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': tmp, 'tool_input': {}}
            for command, expected in [
                ('rtk proxy git status --short --branch', 'git.status'),
                ('rtk git status', 'git.status'),
                ('rtk proxy git diff --stat', 'git.diff'),
                ('rtk proxy git diff --staged --numstat', 'git.staged'),
                ('rtk proxy git branch -vv', 'git.branches'),
                ('rtk proxy git log --oneline -n 3', 'git.log'),
                ('rtk proxy rg --files', 'project.tree'),
                ('rtk proxy rg -n -F calculate src', 'project.search'),
                ('rtk proxy git status;\nrtk proxy touch marker', 'git.status')]:
                with self.subTest(command=command):
                    data['tool_input'] = {'command': command}
                    result = self.invoke(data, [root])
                    self.assertEqual(result['permissionDecision'], 'deny')
                    self.assertIn(expected, result['permissionDecisionReason'])
            self.assertFalse((root / 'marker').exists())
            for command in ('rtk proxy git add file', 'rtk proxy git commit -m change',
                            'rtk proxy git diff', 'rtk proxy git diff --check',
                            'rtk proxy git log --format=%H', 'rtk proxy git branch new-branch',
                            'rtk proxy rg -n "foo|bar" src', 'rtk proxy cat source.py',
                            'rtk proxy python scripts/verify.py',
                            'rtk proxy atelier-routines run git.status --params OBJET_JSON',
                            'rtk proxy git status > status.txt', 'rtk proxy git status | rtk proxy cat'):
                with self.subTest(command=command):
                    data['tool_input'] = {'command': command}
                    self.assertIsNone(self.invoke(data, [root]))
            data['tool_input'] = {'cmd': 'rtk proxy git status', 'workdir': tmp}
            data['tool_name'] = 'exec_command'
            self.assertEqual(self.invoke(data, [root])['permissionDecision'], 'deny')
            self.assertIsNone(self.invoke(data, [root / 'other']))
            data['tool_name'] = 'mcp__atelier_routines__routines_run'
            self.assertIsNone(self.invoke(data, [root]))
            self.assertIsNone(self.invoke({'hook_event_name': 'PreToolUse', 'large': 'x' * 65536}, [root]))

    def test_redirected_parameters_run_through_real_mcp(self):
        asyncio.run(self.check_mcp())

    async def check_mcp(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            async with connection(project) as session:
                for command in ('rtk proxy git status --short --branch', 'rtk proxy git diff --stat',
                                'rtk proxy git diff --cached --numstat', 'rtk proxy git branch',
                                'rtk proxy git log --oneline -3', 'rtk proxy rg --files',
                                'rtk proxy rg -n -F calculate src'):
                    identifier, params = routing_hook.route(shlex.split(command), project, [project])
                    body, response = await rpc(session, 'routines_run', {'id': identifier, 'params': params})
                    self.assertFalse(response.isError, body)
                    self.assertEqual(body['status'], 'ok', body)
