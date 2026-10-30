"""Offline checks: selection and guards only; never run the selected operations."""
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import routines


class RoutinesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = json.loads(routines.CATALOGUE.read_text())
        cls.active = [e for e in cls.entries if e['kind'] != 'proposal']

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = routines.main(list(args))
        return code, output.getvalue()

    def test_catalogue(self):
        self.assertEqual(len(self.active), 109)
        self.assertEqual(len({e['id'] for e in self.entries}), len(self.entries))
        for e in self.active:
            with self.subTest(routine=e['id']):
                self.assertTrue(e['description'] and e['boundary'])
                self.assertTrue(any(e.get(k) for k in ('command', 'steps', 'source', 'template')))
                if e.get('command'):
                    self.assertTrue(e['command'].startswith('rtk '))

    def test_cli_version_uses_the_shared_release(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            routines.main(['--version'])
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), 'atelier-routines ' + routines.VERSION)

    def test_every_routine_is_reachable_without_execution(self):
        with patch.object(routines.subprocess, 'run', side_effect=AssertionError('Unexpected execution')):
            for e in self.active:
                with self.subTest(routine=e['id']):
                    code, output = self.call('show', e['id'])
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(output)['id'], e['id'])
                    self.assertEqual(routines.match(self.entries, e['id'])[0]['id'], e['id'])
                    self.assertLessEqual(len(routines.selection(self.entries, e['id'])), 1950)

    def test_real_requests(self):
        requests = {
            'Le port est occupé': 'web.ports',
            'Je veux voir le statut git': 'git.status',
            'Montre les changements indexés': 'git.staged',
            'Retrouve les appelants de cette fonction': 'project.symbol',
            'Fais les tests unitaires': 'checks.unit',
            'Lance la non régression complète': 'checks.regression',
            'Le test échoué a besoin de diagnostic': 'checks.failure',
            'Vérifie les types': 'checks.types',
            'La CI rouge bloque la PR': 'git.pr-checks',
            'Je veux ouvrir une pull request, créer PR': 'git.pr-create',
            'Vérifie les protections GitHub': 'git.protection',
            'Il y a un merge conflict': 'git.conflict',
            'Chercher code dans ce module': 'project.search',
            'Récupérer résultats VPS': 'system.vps-results',
            'Attendre VPS': 'system.vps-wait',
            'Fais une copie QA': 'system.snapshot',
            'Vérifie le budget temporaire': 'system.budget',
            'Pourquoi la pression mémoire et le swap augmentent': 'system.memory',
            'Vérifie API health': 'web.api-health',
            'Fais un test navigateur E2E': 'web.e2e',
            'Teste la navigation clavier': 'web.accessibility',
            'Regarde les network errors': 'web.network',
            'Résume ce document PDF': 'files.pdf',
            'Analyse ce CSV': 'files.csv',
            'Modifie ce fichier DOCX': 'files.docx',
            'Corrige les formules du tableur': 'files.sheets',
            'Prépare la présentation PowerPoint': 'files.slides',
            'Vérifie cette boucle audio': 'audio.loop',
            'Je veux un export stems': 'audio.stems',
            'Ajuste le modèle audio et son codec': 'audio.model',
            'Je veux un retour écoute': 'audio.feedback',
            'Va chercher le fichier Google Drive': 'apps.drive',
            'Chercher mail dans Gmail': 'apps.mail-read',
            'Prépare un brouillon mail': 'apps.mail-draft',
            'Regarde mon agenda': 'apps.calendar',
            'Crée une nouvelle compétence, créer skill': 'codex.skills',
            'Combien reste-t-il de quota Codex': 'codex.usage',
            'Crée un rappel': 'codex.automation',
            'Générer image du personnage': 'design.image',
            'Reprends la scène Blender': 'design.blender',
        }
        for query, expected in requests.items():
            with self.subTest(query=query):
                selected = routines.match(self.entries, query)
                self.assertTrue(selected)
                self.assertEqual(selected[0]['id'], expected)

    def test_no_match_or_proposal_is_executed(self):
        with patch.object(routines.subprocess, 'run', side_effect=AssertionError('Unexpected execution')):
            self.assertEqual(routines.match(self.entries, 'bonjour merci'), [])
            self.assertEqual(routines.match(self.entries, 'xyzzy quux'), [])
            self.assertEqual(self.call('match', '')[0], 2)
            self.assertEqual(self.call('show', 'unknown')[0], 2)
            self.assertEqual(self.call('match', '$(touch /tmp/unwanted); tests unitaires')[0], 0)
            selected = routines.match(self.entries, 'starter.web')
            self.assertTrue(all(e['kind'] != 'proposal' for e in selected))
            code, output = self.call('list')
            self.assertEqual(code, 0)
            self.assertIn('109 routines', output)
            self.assertLessEqual(len(output), 1950)

    def test_existing_guard_contracts(self):
        with contextlib.redirect_stdout(io.StringIO()):
            routines.self_test()

    def test_legacy_inspections_refuse_symbolic_files(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp) / 'project'
            project.mkdir()
            (Path(tmp) / 'outside.json').write_text('{"scripts":{}}')
            (Path(tmp) / 'outside.py').write_text('def outside(): pass\n')
            (project / 'package.json').symlink_to(Path(tmp) / 'outside.json')
            (project / 'alias.py').symlink_to(Path(tmp) / 'outside.py')
            self.assertEqual(self.call('context', '--project', str(project))[0], 2)
            self.assertEqual(self.call('outline', '--file', str(project / 'alias.py'))[0], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
