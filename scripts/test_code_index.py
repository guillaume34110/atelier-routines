import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import code_index
import executor


class CodeIndexTest(unittest.TestCase):
    def test_portable_index_refreshes_other_projects_without_copying_source(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as temporary:
            root = Path(temporary) / 'workspace'
            app, library = root / 'app', root / 'library'
            app.mkdir(parents=True)
            library.mkdir()
            source = app / 'auth.ts'
            source.write_text('export async function loginUser() { return "private-canary"; }\n')
            (library / 'session.ts').write_bytes(source.read_bytes())
            (library / 'service.py').write_text('def authenticate_user():\n    return True\n')
            (app / 'node_modules').mkdir()
            (app / 'node_modules/hidden.ts').write_text('function hiddenDependency() {}\n')
            (app / 'secret.py').write_text('def hiddenSecret():\n    pass\n')
            (app / 'outside.ts').symlink_to(library / 'session.ts')
            store = Path(temporary) / 'private/definitions'
            with patch.dict(os.environ, {'ATELIER_ROUTINES_ROOTS': json.dumps([str(root)]),
                                         'ATELIER_ROUTINES_STORE': str(store), 'ATELIER_ROUTINES_DECISION': 'off'}):
                indexed = executor.run('code.index', {'project': str(app)})
                self.assertEqual(indexed['status'], 'ok', indexed)
                self.assertEqual(indexed['result']['summary']['indexed_files'], 3)
                found = executor.run('code.find', {'project': str(app), 'pattern': 'loginUser'})
                self.assertEqual(found['status'], 'ok', found)
                self.assertEqual([item['path'] for item in found['result']['details']], [str(source), str(library / 'session.ts')])
                self.assertTrue(all(item['current'] and item['identical_files'] == 2 and item['qa'] == 'not_assessed'
                                    for item in found['result']['details']))
                source.write_text('export function signInUser() { return "private-canary"; }\n')
                self.assertEqual(executor.run('code.find', {'project': str(app), 'pattern': 'loginUser'})['result']['summary']['returned'], 1)
                self.assertEqual(executor.run('code.find', {'project': str(app), 'pattern': 'signInUser'})['result']['details'][0]['path'], str(source))
                (library / 'service.py').unlink()
                self.assertEqual(executor.run('code.index', {'project': str(app)})['result']['summary']['indexed_files'], 2)
                database = code_index.location() / 'index.sqlite3'
                content = database.read_bytes()
                self.assertNotIn(b'private-canary', content)
                self.assertNotIn(b'hiddenSecret', content)
                self.assertEqual(code_index.location().stat().st_mode & 0o777, 0o700)
                self.assertEqual(database.stat().st_mode & 0o777, 0o600)
                with patch.dict(os.environ, {'ATELIER_ROUTINES_ROOTS': json.dumps([str(app)])}):
                    narrowed = executor.run('code.find', {'project': str(app), 'pattern': 'session'})
                    self.assertEqual(narrowed['status'], 'ok', narrowed)
                    self.assertEqual(narrowed['result']['summary']['indexed_files'], 1)
                    self.assertEqual(narrowed['result']['summary']['returned'], 0)
