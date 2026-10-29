"""VPS-only preparation: lock/audit the SDK, then run the complete verification."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    if not os.environ.get('USEVPS_JOB_ID'):
        raise SystemExit('Préparation portable sous le garde du worker Atelier VPS uniquement.')
    workspace = Path.cwd() / 'output/task-tmp'
    workspace.mkdir(parents=True, exist_ok=False)
    os.environ['TASK_WORKSPACE'] = str(workspace)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    commands = [
        ['uv', 'pip', 'compile', 'requirements.txt', '--generate-hashes', '--no-annotate', '-o', str(workspace / 'requirements.lock')],
        ['uv', 'run', '--with', 'pip-audit==2.10.1', 'python', '-m', 'pip_audit', '-r', str(workspace / 'requirements.lock'), '--disable-pip', '--no-deps',
         '-f', 'json', '-o', str(workspace / 'dependencies.json')],
        ['uv', 'run', '--with-requirements', str(workspace / 'requirements.lock'),
         'python', str(Path(__file__).with_name('verify.py'))],
    ]
    for index, argv in enumerate(commands):
        with (workspace / ('prepare-' + str(index) + '.log')).open('w') as log:
            result = subprocess.run(['rtk', 'proxy', *argv], stdout=log, stderr=subprocess.STDOUT, timeout=180)
        if result.returncode:
            print(json.dumps({'passed': False, 'phase': index, 'exit_code': result.returncode}))
            print((workspace / ('prepare-' + str(index) + '.log')).read_text()[-2500:])
            return result.returncode
    print((workspace / 'validation.json').read_text())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
