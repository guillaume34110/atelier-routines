import os
from pathlib import Path
import tempfile
import tomllib
import unittest

import install


class InstallTest(unittest.TestCase):
    def test_preserves_configuration_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            config = Path(tmp) / 'config.toml'
            previous = '# keep comments\nmodel = "existing"\n[mcp_servers.other]\ncommand = "other"\n[mcp_servers.other.env]\nPRIVATE = "synthetic-test-value"\n'
            config.write_text(previous)
            expected = tomllib.loads(previous)
            entry = install.configure(config, '/runtime with spaces/python', Path(tmp), '/usr/bin/rtk')
            self.assertTrue(config.read_text().startswith(previous))
            parsed = tomllib.loads(config.read_text())
            self.assertEqual(parsed['mcp_servers'].pop('atelier-routines'), entry)
            self.assertEqual(parsed, expected)
            installed = config.read_bytes()
            install.configure(config, '/runtime with spaces/python', Path(tmp), '/usr/bin/rtk')
            self.assertEqual(config.read_bytes(), installed)
            updated = install.configure(config, '/updated/python', Path(tmp), '/usr/bin/rtk', replace=True)
            self.assertEqual(updated['args'][1], '/updated/python')
            parsed = tomllib.loads(config.read_text())
            parsed['mcp_servers'].pop('atelier-routines')
            self.assertEqual(parsed, expected)
            installed = config.read_bytes()
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                install.configure(config, '/different/python', Path(tmp), '/usr/bin/rtk')
            self.assertEqual(config.read_bytes(), installed)

    def test_refuses_symbolic_config(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            source = Path(tmp) / 'original.toml'
            source.write_text('# original\n')
            link = Path(tmp) / 'link.toml'
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                install.configure(link, '/python', Path(tmp), '/rtk')
            self.assertEqual(source.read_text(), '# original\n')

    def test_skill_is_discoverable_and_preserves_local_edits(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            source = Path(tmp) / 'source.md'
            source.write_text('name: atelier-routines\n')
            directory = Path(tmp) / 'skill'
            directory.mkdir()
            target = directory / 'SKILL.md'
            target.symlink_to(source)
            install.install_skill(source, directory)
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_text(), source.read_text())
            source.write_text('updated instructions\n')
            install.install_skill(source, directory)
            self.assertEqual(target.read_text(), source.read_text())
            target.write_text('user edits\n')
            with self.assertRaises(ValueError):
                install.install_skill(source, directory)
            self.assertEqual(target.read_text(), 'user edits\n')
