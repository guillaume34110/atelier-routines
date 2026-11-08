import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import code_index
import executor


class KnowledgeTest(unittest.TestCase):
    def test_cross_project_findings_are_scoped_and_evidence_is_checked(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as temporary:
            root = Path(temporary) / 'workspace'
            model, application = root / 'model', root / 'application'
            model.mkdir(parents=True)
            application.mkdir()
            evidence = model / 'evaluation.json'
            evidence.write_text('{"score":0.86,"body":"private-canary"}')
            store = Path(temporary) / 'private/definitions'
            environment = {'ATELIER_ROUTINES_ROOTS': json.dumps([str(root)]),
                           'ATELIER_ROUTINES_STORE': str(store), 'ATELIER_ROUTINES_DECISION': 'off'}
            params = {'project': str(model), 'topic': 'Machine-learning',
                      'question': 'Quel protocole évite de refaire les essais de modèle ?',
                      'conditions': 'Données v2 ; split de test gelé ; GPU 24 Gio.',
                      'finding': 'Un seul entraînement cible a atteint le seuil défini.',
                      'reported_result': 'worked', 'evidence_file': 'evaluation.json'}
            with patch.dict(os.environ, environment):
                first = executor.run('knowledge.record', params)
                self.assertEqual(first['status'], 'ok', first)
                identifier = first['result']['summary']['id']
                self.assertTrue(first['result']['summary']['created'])
                again = executor.run('knowledge.record', params)
                self.assertEqual(again['result']['summary']['id'], identifier)
                self.assertFalse(again['result']['summary']['created'])
                found = executor.run('knowledge.find', {'project': str(application), 'query': 'machine learning'})
                self.assertEqual(found['status'], 'ok', found)
                self.assertEqual(found['result']['summary']['matches_seen'], 1)
                self.assertTrue(found['result']['details'][0]['evidence_current'])
                self.assertFalse(found['result']['summary']['qa_certified'])
                self.assertNotIn(b'private-canary', (code_index.location() / 'index.sqlite3').read_bytes())

                evidence.write_text('{"score":0.72}')
                stale = executor.run('knowledge.find', {'project': str(application), 'query': 'machine learning'})
                self.assertEqual(stale['result']['summary']['changed_evidence'], 1)
                self.assertFalse(stale['result']['details'][0]['evidence_current'])
                wrong_theme = executor.run('knowledge.record', {**params, 'topic': 'Infrastructure',
                    'supersedes': identifier})
                self.assertEqual(wrong_theme['status'], 'error')
                correction = executor.run('knowledge.record', {**params, 'finding': 'Résultat invalidé après correction du split.',
                    'reported_result': 'invalidated', 'supersedes': identifier})
                self.assertEqual(correction['status'], 'ok', correction)
                current = executor.run('knowledge.find', {'project': str(application), 'query': 'machine learning'})
                self.assertEqual(current['result']['summary']['matches_seen'], 1)
                self.assertEqual(current['result']['details'][0]['supersedes'], identifier)
                self.assertTrue(current['result']['details'][0]['evidence_current'])

                (model / 'secret.json').write_text('{}')
                denied = executor.run('knowledge.record', {**params, 'evidence_file': 'secret.json'})
                self.assertEqual(denied['status'], 'error')
                with patch.dict(os.environ, {'ATELIER_ROUTINES_ROOTS': json.dumps([str(application)])}):
                    scoped = executor.run('knowledge.find', {'project': str(application), 'query': 'machine learning'})
                    self.assertEqual(scoped['status'], 'ok', scoped)
                    self.assertEqual(scoped['result']['summary']['matches_seen'], 0)
