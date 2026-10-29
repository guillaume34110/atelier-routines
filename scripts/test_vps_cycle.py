import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import vps_cycle


class InstalledClientTest(unittest.TestCase):
    def test_exact_installed_launcher_and_refused_shell_code(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            home = Path(tmp)
            launcher = home / '.local/bin/atelier-vps'
            launcher.parent.mkdir(parents=True)
            script = home / 'client with spaces/vps.py'
            script.parent.mkdir()
            script.write_text('# synthetic client\n')
            with patch.object(Path, 'home', return_value=home):
                self.assertIsNone(vps_cycle.client_path())
                launcher.write_text(f'#!/bin/sh\nexec /usr/bin/python3 "{script}" "$@"\n')
                self.assertEqual(vps_cycle.client_path(), script)
                launcher.write_text(f'#!/bin/sh\nexec /usr/bin/python3 "{script}" "$@"; touch marker\n')
                self.assertIsNone(vps_cycle.client_path())
                launcher.write_text(f'#!/bin/sh\neval "{script}"\n')
                self.assertIsNone(vps_cycle.client_path())
                self.assertFalse((home / 'marker').exists())
