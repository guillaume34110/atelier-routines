"""Dependency and redacted secret checks; fail closed without ignoring findings."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    workspace = Path(os.environ['TASK_WORKSPACE'])
    commands = [
        ['rtk', 'proxy', sys.executable, '-m', 'pip_audit', '-r', 'requirements.lock', '--disable-pip', '--no-deps',
         '--format', 'json', '--output', str(workspace / 'dependencies.json')],
        ['rtk', 'proxy', 'gitleaks', 'git', '.', '--redact', '--no-banner', '--log-opts=--all',
         '--report-format', 'json', '--report-path', str(workspace / 'secret-scan.json')],
    ]
    for index, argv in enumerate(commands):
        with (workspace / ('security-' + str(index) + '.log')).open('w') as log:
            result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        if result.returncode:
            print(json.dumps({'passed': False, 'phase': index, 'exit_code': result.returncode}))
            return result.returncode
    print(json.dumps({'passed': True, 'checks': ['locked_dependencies', 'git_history_secrets']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
