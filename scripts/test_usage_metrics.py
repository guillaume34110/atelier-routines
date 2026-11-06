import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import usage


class TokenUsageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE'])
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.home = self.project / 'codex'
        self.sessions = self.home / 'sessions/2026/10/31'
        self.sessions.mkdir(parents=True)
        self.path = self.sessions / 'rollout-fixture.jsonl'
        environment = patch.dict(os.environ, {'ATELIER_ROUTINES_STORE': str(self.project / 'definitions'),
            'ATELIER_ROUTINES_USAGE': 'on', 'CODEX_HOME': str(self.home), 'CODEX_THREAD_ID': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def event(self, category, payload):
        return {'timestamp': '2026-10-31T10:00:00Z', 'type': category, 'payload': payload}

    def header(self, turn='private-turn'):
        return [self.event('session_meta', {'id': 'private-session', 'cwd': '/private/canary'}),
                self.event('event_msg', {'type': 'task_started', 'turn_id': turn}),
                self.event('turn_context', {'turn_id': turn})]

    def counts(self, inputs, cached, outputs, reasoning=0):
        return dict(zip(usage.TOKEN_KEYS, [inputs, cached, 0, outputs, reasoning, inputs + outputs]))

    def tokens(self, total, last=None):
        return self.event('event_msg', {'type': 'token_count', 'info': {
            'total_token_usage': total, 'last_token_usage': last if last is not None else total},
            'rate_limits': {'irrelevant': 'private-canary'}})

    def append(self, *events):
        with self.path.open('ab') as output:
            for event in events:
                output.write(json.dumps(event).encode() + b'\n')

    def token_report(self):
        return usage.report(20)['tokens']

    def test_real_counters_batches_deduplication_and_private_content(self):
        sample = self.counts(100, 60, 10, 3)
        self.append(*self.header(), self.event('response_item', {'type': 'reasoning', 'content': 'private-reasoning-canary'}),
            self.event('response_item', {'type': 'function_call', 'name': 'read', 'call_id': 'private-call', 'arguments': 'private-arguments-canary'}),
            self.event('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'private-call-2', 'input': 'private-command-canary'}),
            self.tokens(sample), self.tokens(sample),
            self.event('event_msg', {'type': 'task_complete', 'turn_id': 'private-turn', 'last_agent_message': 'private-message-canary'}))
        self.assertEqual(usage.collect_tokens(self.path)['new_steps'], 1)
        result = self.token_report()
        self.assertEqual(result['state'], 'observed')
        self.assertEqual(result['totals']['total_tokens'], 110)
        self.assertEqual(result['totals']['uncached_input_tokens'], 40)
        self.assertEqual(result['totals']['reasoning_output_tokens'], 3)
        self.assertEqual(result['steps'][0]['operation'], 'tool_batch')
        self.assertEqual(result['steps'][0]['tool_calls'], 2)
        self.assertTrue(result['tasks'][0]['turn_closed'])
        self.assertEqual(usage.collect_tokens(self.path)['new_steps'], 0)
        content = (usage.root() / 'events.sqlite3').read_bytes()
        for value in (b'private-session', b'private-turn', b'private-call', b'private-canary', b'private-reasoning-canary',
                      b'private-arguments-canary', b'private-command-canary', b'private-message-canary'):
            self.assertNotIn(value, content)

    def test_incremental_partial_line_and_exact_task_assignment(self):
        first = self.counts(100, 80, 20, 10)
        second = self.counts(160, 100, 30, 15)
        delta = self.counts(60, 20, 10, 5)
        self.append(*self.header('first'), self.tokens(first),
                    self.event('event_msg', {'type': 'task_complete', 'turn_id': 'first'}),
                    self.event('event_msg', {'type': 'task_started', 'turn_id': 'second'}),
                    self.event('response_item', {'type': 'function_call', 'name': 'mcp__atelier_routines__routines_run',
                                               'call_id': 'second-call', 'arguments': json.dumps({'id': 'project.search', 'params': {'token': 'private-value'}})}))
        self.assertEqual(usage.collect_tokens(self.path)['new_steps'], 1)
        line = json.dumps(self.tokens(second, delta)).encode() + b'\n'
        with self.path.open('ab') as output:
            output.write(line[:-9])
        self.assertEqual(usage.collect_tokens(self.path)['status'], 'partial')
        self.assertEqual(self.token_report()['totals']['total_tokens'], 120)
        with self.path.open('ab') as output:
            output.write(line[-9:])
        self.assertEqual(usage.collect_tokens(self.path)['new_steps'], 1)
        usage.record('hook', 'prompt', 'unknown', session='private-session', turn_id='second', prompt='Chercher les appels')
        result = self.token_report()
        self.assertEqual(result['totals']['total_tokens'], 190)
        self.assertEqual(sorted(t['total_tokens'] for t in result['tasks']), [70, 120])
        task = next(t for t in result['tasks'] if t['total_tokens'] == 70)
        self.assertEqual(task['instruction'], 'Chercher les appels')
        self.assertFalse(task['turn_closed'])
        self.assertIn('project.search', [s['operation'] for s in result['steps']])

    def test_missing_baseline_and_counter_reset_remain_partial(self):
        self.append(*self.header(), self.tokens(self.counts(500, 300, 40, 10), self.counts(100, 80, 10, 3)))
        usage.collect_tokens(self.path)
        self.append(self.tokens(self.counts(10, 0, 2)), self.tokens(self.counts(15, 2, 5, 1), self.counts(5, 2, 3, 1)))
        usage.collect_tokens(self.path)
        result = self.token_report()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['totals']['total_tokens'], 118)

    def test_gap_across_turns_is_unassigned_instead_of_invented(self):
        self.append(*self.header('first'), self.tokens(self.counts(100, 50, 10)),
                    self.event('event_msg', {'type': 'task_started', 'turn_id': 'second'}),
                    self.tokens(self.counts(300, 150, 30), self.counts(50, 20, 5)))
        usage.collect_tokens(self.path)
        result = self.token_report()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['unassigned_steps'], 1)
        self.assertEqual(result['aggregate_intervals'], 1)
        self.assertEqual(result['totals']['total_tokens'], 330)
        self.assertEqual(len(result['tasks']), 1)

    def test_replayed_source_is_not_added_twice_and_rewrite_is_detected(self):
        self.append(*self.header(), self.tokens(self.counts(100, 40, 10)))
        usage.collect_tokens(self.path)
        copy = self.sessions / 'copy.jsonl'
        copy.write_bytes(self.path.read_bytes())
        self.assertEqual(usage.collect_tokens(copy)['new_steps'], 0)
        self.assertEqual(self.token_report()['totals']['total_tokens'], 110)
        with self.path.open('wb') as output:
            output.write(b' ' * (self.path.stat().st_size + 2048))
        self.assertEqual(usage.collect_tokens(self.path)['status'], 'source_changed')
        self.assertEqual(self.token_report()['totals']['total_tokens'], 110)

    def test_sources_outside_sessions_and_links_are_refused(self):
        self.append(*self.header(), self.tokens(self.counts(10, 0, 1)))
        outside = self.project / 'outside.jsonl'
        outside.write_bytes(self.path.read_bytes())
        self.assertEqual(usage.collect_tokens(outside)['status'], 'unavailable')
        link = self.sessions / 'link.jsonl'
        link.symlink_to(self.path)
        self.assertEqual(usage.collect_tokens(link)['status'], 'unavailable')
        hard = self.sessions / 'hard.jsonl'
        os.link(self.path, hard)
        self.assertEqual(usage.collect_tokens(hard)['status'], 'unavailable')
        self.assertFalse(usage.root().exists())

    def test_read_budget_and_oversized_record_are_bounded(self):
        self.append(*self.header(), self.event('response_item', {'type': 'function_call_output', 'output': 'x' * 2300}),
                    self.tokens(self.counts(100, 40, 10)))
        with patch.object(usage, 'TOKEN_SCAN_BYTES', 2048), patch.object(usage, 'TOKEN_LINE_BYTES', 1024):
            for _ in range(3):
                self.assertLessEqual(usage.collect_tokens(self.path)['bytes_read'], 2048)
        result = self.token_report()
        self.assertEqual(result['totals']['total_tokens'], 110)
        self.assertEqual(result['state'], 'partial')

    def test_invalid_or_missing_counts_never_become_zero_usage(self):
        invalid = self.counts(10, 11, 1)
        self.append(*self.header(), self.tokens(self.counts(0, 0, 0)), self.tokens(invalid))
        self.assertEqual(usage.collect_tokens(self.path)['status'], 'partial')
        self.assertEqual(self.token_report()['state'], 'unavailable')
        self.assertIsNone(self.token_report()['totals'])
        with patch.dict(os.environ, {'ATELIER_ROUTINES_USAGE': 'off'}):
            self.assertEqual(usage.collect_tokens(self.path)['status'], 'disabled')

    def test_current_uuid7_source_and_stop_hook_use_numeric_data_only(self):
        identifier = '017f22e2-79b0-7cc3-98c4-dc0c0c07398f'
        directory = self.home / 'sessions/2022/02/22'
        directory.mkdir(parents=True)
        self.path = directory / ('rollout-' + identifier + '.jsonl')
        self.append(*self.header(), self.tokens(self.counts(30, 20, 4, 2)))
        with patch.dict(os.environ, {'CODEX_THREAD_ID': identifier}):
            self.assertEqual(usage.current_token_source(), self.path)
        data = {'hook_event_name': 'Stop', 'session_id': 'private-session', 'turn_id': 'private-turn',
                'cwd': str(self.project), 'transcript_path': str(self.path)}
        result = subprocess.run(['rtk', 'proxy', sys.executable, str(Path(usage.__file__)), '--hook', json.dumps([str(self.project)])],
                                input=json.dumps(data), text=True, capture_output=True, timeout=5, check=True)
        self.assertEqual((result.stdout, result.stderr), ('', ''))
        self.assertEqual(self.token_report()['totals']['total_tokens'], 34)
        self.assertLessEqual(len(json.dumps(usage.report(1))), 8000)
