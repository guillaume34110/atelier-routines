#!/usr/bin/env python3
"""Bound temporary QA copies, task outputs and captured logs. Never delete existing work."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

GIB = 1024 ** 3
QA_LIMIT = 2 * GIB
TEMP_LIMIT = 5 * GIB
LOG_LIMIT = 20 * 1024 ** 2
AUTHORIZED_RESERVE_GIB = None
GUARD = Path(__file__).with_name('storage-guard.py')
EXCLUDED = {'.git', 'node_modules', 'output', 'outputs', 'dist', 'build', 'target',
            '.cache', 'cache', 'caches', '__pycache__', '.venv', 'venv', 'archives',
            'archive', 'backups', 'backup', 'coverage', '.next', '.nuxt'}
ARCHIVES = ('.zip', '.tar', '.tar.gz', '.tgz', '.tar.zst', '.7z', '.dmg')


def temporary_budget(value):
    try:
        gib = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('temporary budget must be a number of GiB')
    if not 1 / 1024 <= gib <= 5:
        raise argparse.ArgumentTypeError('temporary budget must be between 1 MiB and 5 GiB')
    return int(gib * GIB)


def excluded(name):
    lower = name.casefold()
    return (lower in EXCLUDED or lower == 'claude.md' or lower.startswith(('.env', '.venv-', '.playwright'))
            or lower.startswith('dist-') or lower.endswith(ARCHIVES))


def guard(path, extra):
    parent = path
    while not parent.exists():
        parent = parent.parent
    command = ['rtk', 'proxy', sys.executable, str(GUARD), '--volume',
               str(parent), '--require-gib', str(extra / GIB)]
    if AUTHORIZED_RESERVE_GIB is not None:
        command += ['--authorized-reserve-gib', str(AUTHORIZED_RESERVE_GIB)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or result.stderr.strip())


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def copy_inputs(project):
    files, total = [], 0
    for base, directories, names in os.walk(project, followlinks=False):
        directories[:] = sorted(d for d in directories
            if not excluded(d) and not Path(base, d).is_symlink())
        for name in sorted(names):
            path = Path(base, name)
            if excluded(name) or path.is_symlink():
                continue
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                continue
            total += max(before.st_size, before.st_blocks * 512)
            if total > QA_LIMIT:
                raise RuntimeError('STOP: QA input budget exceeds 2 GiB; serve the existing project/build')
            files.append((path, before.st_ino, before.st_size, before.st_mtime_ns))
    return files, total


def snapshot(project):
    if (project / 'output').is_symlink():
        raise RuntimeError('STOP: output must not be a symlink')
    destination = project / 'output/qa-active'
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('STOP: qa-active already exists; reuse it or review its contents before removal')
    files, total = copy_inputs(project)
    guard(destination.parent, total + 16 * 1024 ** 2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('.qa-active-building-' + str(os.getpid()))
    assert not temporary.exists()
    clone = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True).clonefile
    clone.argtypes, clone.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int
    try:
        temporary.mkdir()
        receipt = []
        for source, inode, size, modified in files:
            before = source.lstat()
            if source.is_symlink() or (before.st_ino, before.st_size, before.st_mtime_ns) != (inode, size, modified):
                raise RuntimeError('source changed during snapshot: ' + str(source))
            relative = source.relative_to(project)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if clone(os.fsencode(source), os.fsencode(target), 0):
                raise RuntimeError('APFS clone unavailable; no physical-copy fallback: ' + str(source))
            digest = sha(source)
            if sha(target) != digest:
                raise RuntimeError('snapshot hash mismatch: ' + str(source))
            shutil.copystat(source, target, follow_symlinks=False)
            receipt.append({'path': str(relative), 'sha256': digest, 'bytes': size})
        (temporary / '.qa-copy.json').write_text(json.dumps({'source': str(project), 'files': receipt}) + '\n')
        if usage(temporary)[0] > QA_LIMIT:
            raise RuntimeError('STOP: QA copy including its receipt exceeds 2 GiB')
        temporary.rename(destination)
        print(json.dumps({'snapshot': str(destination), 'files': len(files), 'budgeted_bytes': total,
                          'max_bytes': QA_LIMIT, 'independent_apfs_clones': True}))
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def usage(root):
    total = logs = 0
    if not root.exists():
        return 0, 0
    for base, directories, names in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if not Path(base, d).is_symlink()]
        for name in names:
            path = Path(base, name)
            if path.is_symlink() or name.casefold() == 'claude.md':
                continue
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += max(st.st_size, st.st_blocks * 512)
                if path.suffix in {'.log', '.jsonl'}:
                    logs += st.st_size
    return total, logs


def stop_owned(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def run(project, command):
    if (project / 'output').is_symlink():
        raise RuntimeError('STOP: output must not be a symlink')
    work = project / 'output/task-tmp'
    if work.is_symlink() or (work.exists() and any(work.iterdir())):
        raise RuntimeError('STOP: task-tmp contains previous work; preserve useful results and review it first')
    if usage(project / 'output/qa-active')[0] > QA_LIMIT:
        raise RuntimeError('STOP: existing qa-active exceeds 2 GiB')
    guard(work, TEMP_LIMIT)
    work.mkdir(parents=True, exist_ok=True)
    (work / 'tmp').mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment['TMPDIR'] = str(work / 'tmp')
    environment['TASK_WORKSPACE'] = str(work)
    process = subprocess.Popen(command, cwd=project, env=environment, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    written, reason, checked = 0, None, 0.0
    try:
        with (work / 'stdout.log').open('wb') as log:
            while selector.get_map():
                for key, _ in selector.select(timeout=0.25):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, LOG_LIMIT - written)
                    log.write(block[:available])
                    written += min(len(block), available)
                    if len(block) > available:
                        reason = 'captured log exceeds 20 MiB'
                if time.monotonic() - checked > 1:
                    log.flush()
                    temporary, logs = usage(work)
                    qa, _ = usage(project / 'output/qa-active')
                    if temporary > TEMP_LIMIT or qa > QA_LIMIT or logs > LOG_LIMIT:
                        reason = 'temporary, QA or combined log budget exceeded'
                    guard(work, 0)
                    checked = time.monotonic()
                if reason:
                    stop_owned(process)
                    break
        code = process.wait()
        temporary, logs = usage(work)
        qa, _ = usage(project / 'output/qa-active')
        if temporary > TEMP_LIMIT or qa > QA_LIMIT or logs > LOG_LIMIT:
            reason = reason or 'temporary, QA or combined log budget exceeded'
        guard(work, 0)
        print(json.dumps({'exit_code': 2 if reason else code, 'child_exit_code': code, 'budget_stop': reason,
                          'workdir': str(work), 'captured_log_bytes': written,
                          'temporary_limit_bytes': TEMP_LIMIT, 'temporary_bytes': temporary}))
        return 2 if reason else code
    finally:
        selector.close()
        process.stdout.close()
        stop_owned(process)


def self_test():
    global QA_LIMIT, TEMP_LIMIT, LOG_LIMIT
    assert excluded('CLAUDE.md') and excluded('output') and excluded('node_modules')
    assert excluded('dist-qa') and excluded('.venv-model') and not excluded('src')
    assert temporary_budget('1') == GIB and temporary_budget('5') == 5 * GIB
    for invalid in ['0', '-1', '6', 'nan', 'inf', 'invalid']:
        try:
            temporary_budget(invalid)
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError('invalid or raised temporary budget accepted: ' + invalid)
    original_limits = QA_LIMIT, TEMP_LIMIT, LOG_LIMIT
    try:
        QA_LIMIT, TEMP_LIMIT, LOG_LIMIT = 2048, 65536, 1024
        with tempfile.TemporaryDirectory(prefix='workspace-budget-check-') as name:
            project = Path(name)
            (project / 'src').mkdir()
            (project / 'src/input.js').write_text('source')
            (project / 'output').mkdir()
            (project / 'output/never-copy.txt').write_text('ignored')
            (project / 'link').symlink_to(project / 'src/input.js')
            # Account for the filesystem minimum block size in this tiny fixture.
            QA_LIMIT = 8192
            target = snapshot(project)
            assert not (target / 'output').exists() and not (target / 'link').exists()
            (target / 'src/input.js').write_text('independent')
            assert (project / 'src/input.js').read_text() == 'source'
            try:
                snapshot(project)
            except RuntimeError:
                pass
            else:
                raise AssertionError('existing snapshots must never be overwritten')
            QA_LIMIT = 1
            try:
                copy_inputs(project)
            except RuntimeError:
                pass
            else:
                raise AssertionError('QA byte cap must reject oversized inputs')
        with tempfile.TemporaryDirectory(prefix='workspace-budget-log-check-') as name:
            started = time.monotonic()
            code = run(Path(name), ['rtk', 'proxy', sys.executable, '-c',
                'import time;print("x"*8192,flush=True);time.sleep(20)'])
            assert code == 2
            assert time.monotonic() - started < 5, 'over-budget command was not stopped promptly'
            assert (Path(name) / 'output/task-tmp/stdout.log').stat().st_size <= LOG_LIMIT
        with tempfile.TemporaryDirectory(prefix='workspace-budget-temp-check-') as name:
            started = time.monotonic()
            code = run(Path(name), ['rtk', 'proxy', sys.executable, '-c',
                'import os,time;from pathlib import Path;Path(os.environ["TASK_WORKSPACE"],"oversize").write_bytes(b"x"*80000);time.sleep(20)'])
            assert code == 2
            assert time.monotonic() - started < 5, 'over-budget writer was not stopped promptly'
        with tempfile.TemporaryDirectory(prefix='workspace-budget-success-check-') as name:
            assert run(Path(name), ['rtk', 'proxy', sys.executable, '-c', 'print("ok")']) == 0
        print('PASS: exclusions, no recursion, independent clones, no overwrite, QA/log/temp caps and normal completion')
    finally:
        QA_LIMIT, TEMP_LIMIT, LOG_LIMIT = original_limits


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temp-gib', dest='temp_limit', type=temporary_budget, default=TEMP_LIMIT,
                        help='stricter run budget in GiB, from 1 MiB to 5 GiB (default: 5)')
    parser.add_argument('--authorized-reserve-gib', type=float,
                        help='explicitly user-authorized reserve for this run and named project volume only')
    parser.add_argument('mode', choices=['snapshot', 'run', 'self-test'])
    parser.add_argument('project', nargs='?', type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.temp_limit != TEMP_LIMIT and args.mode != 'run':
            parser.error('--temp-gib only applies to run')
        if args.authorized_reserve_gib is not None and args.mode != 'run':
            parser.error('--authorized-reserve-gib only applies to run')
        AUTHORIZED_RESERVE_GIB = args.authorized_reserve_gib
        TEMP_LIMIT = args.temp_limit
        if args.mode == 'self-test':
            self_test()
        else:
            if args.project is None:
                parser.error('project must be an existing directory')
            project = args.project.resolve()
            if not project.is_dir():
                parser.error('project must be an existing directory')
            if args.mode == 'snapshot':
                snapshot(project)
            else:
                command = args.command[1:] if args.command[:1] == ['--'] else args.command
                if not command:
                    parser.error('run requires a command after --')
                raise SystemExit(run(project, command))
    except (OSError, RuntimeError) as exc:
        parser.exit(2, str(exc) + '\n')
