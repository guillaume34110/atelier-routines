"""Agents register bounded recipes; execution still crosses the real MCP transport."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from test_mcp import connection, make_fixture, rpc


class WorkflowTest(unittest.TestCase):
    def test_recipe_lifecycle_and_execution(self):
        asyncio.run(self.check_recipe())

    async def check_recipe(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = make_fixture(tmp)
            recipe = {'project': str(project), 'name': 'project-review', 'description': 'Contexte et références du projet',
                      'inputs': {'term': {'type': 'string', 'default': 'calculate'}},
                      'steps': [{'id': 'git.status', 'params': {}},
                                {'id': 'project.search', 'params': {'pattern': {'$input': 'term'}, 'path': 'src'}}]}
            async with connection(project) as session:
                body, response = await rpc(session, 'routines_run', {'id': 'routines.register', 'params': recipe})
                self.assertEqual(body['status'], 'ok', body)
                self.assertFalse(response.isError)
                self.assertFalse(body['result']['summary']['executed'])
                found, _ = await rpc(session, 'routines_find', {'query': 'custom.project-review', 'limit': 1})
                self.assertEqual(found['matches'][0]['id'], 'custom.project-review')
                self.assertEqual(found['matches'][0]['params']['properties']['term']['default'], 'calculate')
                body, response = await rpc(session, 'routines_run', {'id': 'custom.project-review', 'params': {'project': str(project)}})
                self.assertEqual(body['status'], 'ok', body)
                proof = json.loads(Path(body['evidence'][0]).read_text())
                self.assertEqual(proof['result']['summary']['steps_completed'], 2)
                self.assertEqual(proof['result']['details'][1]['summary']['matches'], 1)
                self.assertEqual(body['internal_commands'], 4)
                listed, _ = await rpc(session, 'routines_run', {'id': 'routines.list', 'params': {'project': str(project)}})
                receipt = json.loads(Path(listed['evidence'][0]).read_text())
                self.assertEqual(receipt['result']['details'][0]['last_run']['status'], 'ok')
                management = {'project': str(project), 'name': 'project-review', 'action': 'disable'}
                body, _ = await rpc(session, 'routines_run', {'id': 'routines.manage', 'params': management})
                self.assertFalse(body['result']['summary']['enabled'])
                repeated, _ = await rpc(session, 'routines_run', {'id': 'routines.register', 'params': recipe})
                self.assertFalse(repeated['result']['summary']['enabled'])
                body, response = await rpc(session, 'routines_run', {'id': 'custom.project-review', 'params': {'project': str(project)}})
                self.assertEqual(body['status'], 'unavailable', body)
                self.assertTrue(response.isError)
                management['action'] = 'enable'
                body, _ = await rpc(session, 'routines_run', {'id': 'routines.manage', 'params': management})
                self.assertTrue(body['result']['summary']['enabled'])
                management['action'] = 'show'
                shown, _ = await rpc(session, 'routines_run', {'id': 'routines.manage', 'params': management})
                self.assertEqual(shown['status'], 'ok')
                changed = {**recipe, 'description': 'different'}
                body, response = await rpc(session, 'routines_run', {'id': 'routines.register', 'params': changed})
                self.assertEqual(body['status'], 'error', body)
                for changes in (
                    {'name': '../escape'}, {'steps': recipe['steps'] * 5},
                    {'inputs': {'x': {'type': 'object'}}},
                    {'steps': [{'id': 'routines.manage', 'params': {}}]},
                    {'steps': [{'id': 'custom.project-review', 'params': {}}]},
                    {'steps': [{'id': 'shell', 'params': {'command': 'arbitrary'}}]},
                    {'steps': [{'id': 'git.status', 'params': {'project': '/etc'}}]},
                    {'steps': [{'id': 'project.search', 'params': {'pattern': {'$input': 'missing'}}}]},
                    {'steps': [{'id': 'git.log', 'params': {'limit': '8; unsafe'}}]},
                ):
                    invalid = {**recipe, **changes}
                    body, response = await rpc(session, 'routines_run', {'id': 'routines.register', 'params': invalid})
                    self.assertEqual(body['status'], 'error', body)
                    self.assertTrue(response.isError)
                failing = {**recipe, 'name': 'first-error-stops', 'inputs': {},
                           'steps': [{'id': 'files.json', 'params': {'file': 'missing.json'}},
                                     {'id': 'git.status', 'params': {}}]}
                body, _ = await rpc(session, 'routines_run', {'id': 'routines.register', 'params': failing})
                self.assertEqual(body['status'], 'ok')
                body, response = await rpc(session, 'routines_run', {'id': 'custom.first-error-stops', 'params': {'project': str(project)}})
                self.assertEqual(body['status'], 'error', body)
                self.assertTrue(response.isError)
                proof = json.loads(Path(body['evidence'][0]).read_text())
                self.assertEqual(len(proof['result']['details']), 1)
                self.assertEqual(proof['result']['summary']['steps_completed'], 0)
                for params in ({'project': str(project), 'term': 'x', 'command': 'no'},
                               {'project': str(project), 'term': []},
                               {'project': str(project), 'term': 'x' * 1025}):
                    body, response = await rpc(session, 'routines_run', {'id': 'custom.project-review', 'params': params})
                    self.assertEqual(body['status'], 'error')
                    self.assertTrue(response.isError)
