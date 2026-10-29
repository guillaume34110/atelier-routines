"""One authorised VPS adapter: this project's complete verification, with receipts."""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import sys
import time
import uuid

import executor as core
import executions

TASKS = {}
PROCESSES = {}
HERE = Path(__file__).resolve().parent
PATHS = ['receipt.json', 'stdout.log', 'work/output/task-tmp/validation.json',
         'work/output/task-tmp/tests.log', 'work/output/task-tmp/benchmark.json']
COMMAND = ['rtk', 'proxy', 'uv', 'run', '--with-requirements', 'requirements.lock', 'python', 'scripts/verify.py']


def client_path():
    path = Path.home() / '.local/bin/atelier-vps'
    if not path.is_file():
        return None
    if path.resolve().name == 'vps.py':
        return path.resolve()
    with path.open('rb') as source:
        content = source.read(4097)
    if len(content) > 4096:
        return None
    try:
        lines = content.decode().strip().splitlines()
        words = shlex.split(lines[1]) if len(lines) == 2 and lines[0] == '#!/bin/sh' else []
        if len(words) == 4 and words[0] == 'exec' and words[-1] == '$@':
            script = Path(words[2])
            if script.is_absolute() and script.name == 'vps.py' and script.is_file():
                return script.resolve()
    except (ValueError, UnicodeError):
        pass
    return None


def client():
    path = client_path()
    if path is None or path.name != 'vps.py':
        raise core.Refusal('Client Atelier VPS installé requis ; aucune configuration personnelle transférée.', 'unavailable')
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('atelier_routines_vps_client', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_signature(project, module):
    digest = hashlib.sha256()
    count = total = 0
    for path, name in module.inputs(project):
        data = core.read_bytes(project, name, core.MAX_INPUT)
        total += len(data)
        count += 1
        if total > 32 * 1024 * 1024 or count > 5000:
            raise core.Refusal('Adapter limité à 32 Mio / 5 000 sources.')
        digest.update(name.encode())
        digest.update(hashlib.sha256(data).digest())
    revision, _ = core.git(project, 'rev-parse', 'HEAD')
    return {'sha256': digest.hexdigest(), 'revision': revision.strip(), 'files': count}


def check_project(project):
    if project != HERE.parent or not (project / 'requirements.lock').is_file() or not (project / 'scripts/verify.py').is_file():
        raise core.Refusal('Cet adapter vérifie uniquement le projet atelier-routines installé ; autres projets guidés.', 'unavailable')


async def start(project, params):
    check_project(project)
    module = client()
    source = source_signature(project, module)
    execution_id, existing = executions.begin('checks.regression', {'project': str(project), 'source': source})
    if existing is not None:
        return core.compact(existing)
    executions.update(execution_id, source=source, state='running', validation_status='pending',
                      stages=['parameters_validated', 'source_fingerprinted'])
    task = asyncio.create_task(pipeline(execution_id, project, 'start'))
    TASKS[execution_id] = task
    task.add_done_callback(lambda finished: TASKS.pop(execution_id, None))
    if params['wait']:
        return await wait(project, execution_id, 3600)
    return executions.verified_view(executions.get(execution_id, project))


async def wait(project, execution_id, seconds):
    record = executions.get(execution_id, project)
    task = TASKS.get(execution_id)
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), seconds)
        except asyncio.TimeoutError:
            return core.compact(executions.verified_view(executions.get(execution_id, project)))
        finally:
            if task.done():
                TASKS.pop(execution_id, None)
    return core.compact(executions.verified_view(executions.get(execution_id, project)))


async def resume(project, execution_id):
    record = executions.get(execution_id, project)
    if record['id'] != 'checks.regression' or not record.get('remote_job') or record.get('released'):
        raise core.Refusal('Reprise disponible uniquement pour un calcul VPS identifié, non libéré ; aucune soumission répétée.', 'decision_required')
    if execution_id in TASKS:
        return await wait(project, execution_id, 3600)
    if record['state'] in executions.ACTIVE and executions.alive(record.get('owner_pid')):
        return core.compact(executions.view(record))
    await clean_temporary(project, execution_id, failed=True)
    executions.update(execution_id, owner_pid=os.getpid(), state='running', decision=None)
    TASKS[execution_id] = asyncio.create_task(pipeline(execution_id, project, 'resume'))
    TASKS[execution_id].add_done_callback(lambda finished: TASKS.pop(execution_id, None))
    return await wait(project, execution_id, 3600)


async def clean_temporary(project, execution_id, failed=False):
    temporary = project / 'output/task-tmp'
    if not temporary.exists():
        return
    marker = temporary / '.routines-owner'
    if temporary.is_symlink() or not marker.is_file() or marker.read_text() != execution_id:
        raise core.Refusal('Espace temporaire existant non attribué à cette exécution ; préservé.', 'decision_required')
    if failed:
        # Keep only identified logs and metadata before an explicit, supported recovery.
        evidence = project / 'output/routines-runs' / execution_id
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ('stdout.log', 'fetch.tar'):
            path = temporary / name
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 20 * 1024 * 1024:
                target = evidence / ('interrupted-' + str(time.time_ns()) + '-' + name)
                with path.open('rb') as source, target.open('xb') as out:
                    shutil.copyfileobj(source, out)
                target.chmod(0o600)
    shutil.rmtree(temporary)


async def pipeline(execution_id, project, mode):
    guard = core.routines.BIN / 'workspace-budget.py'
    if not guard.is_file():
        guard = HERE / 'guards/workspace-budget.py'
    args = ['rtk', 'proxy', sys.executable, str(guard), '--temp-gib', '0.6', 'run', str(project), '--',
            'rtk', 'proxy', sys.executable, str(HERE / 'vps_cycle.py'), '_bridge', execution_id, mode]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT, process_group=0)
        PROCESSES[execution_id] = process
        output, _ = await asyncio.wait_for(process.communicate(), 3700)
        record = executions.get(execution_id, project)
        if record['state'] in executions.ACTIVE:
            executions.update(execution_id, state='decision_required' if record.get('remote_job') else 'error',
                              decision='Cycle incomplet ; preuves et calcul distant préservés. ' + core.redact(output.decode(errors='replace'))[:180],
                              owner_pid=None)
        elif process.returncode:
            executions.update(execution_id, guard_exit_code=process.returncode)
        if executions.get(execution_id, project).get('released'):
            await clean_temporary(project, execution_id)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            executions.update(execution_id, state='decision_required', owner_pid=None,
                              decision='Attente interrompue ; aucune nouvelle soumission ni libération automatique.')
        finally:
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    await asyncio.wait_for(process.wait(), 12)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
    except (OSError, ValueError, core.Refusal) as error:
        executions.update(execution_id, state='decision_required', owner_pid=None, decision=core.redact(str(error))[:220])
    finally:
        PROCESSES.pop(execution_id, None)


async def shutdown():
    tasks = list(TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def bridge(execution_id, mode):
    record = executions.get(execution_id, Path.cwd())
    project = core.project_path(record['project'])
    check_project(project)
    temporary = Path(os.environ['TASK_WORKSPACE'])
    (temporary / '.routines-owner').write_text(execution_id)
    executions.update(execution_id, temporary_workspace=str(temporary))
    module = client()
    agent = 'routines-' + execution_id[:16]
    if mode == 'start':
        status = module.fleet_status()
        if not status.get('ok') or not status.get('admit'):
            raise core.Refusal('Admission VPS refusée ; aucun calcul lancé.')
        executions.update(execution_id, submission_attempts=record.get('submission_attempts', 0) + 1,
                          stages=['parameters_validated', 'source_fingerprinted', 'admission_verified', 'submitting'])
        request = {'argv': COMMAND, 'agent': agent, 'project': project.name, 'timeout': 900,
                   'cpus': 1, 'ram_bytes': None, 'disk_bytes': int(.8 * module.GIB),
                   'dependency_digest': module.dependency_digest(project)}
        code, receipt = module.run_on_server(request, project, arch='any')
        if not receipt.get('job_ref'):
            raise core.Refusal('Soumission sans référence certaine ; préserver le travail.', 'decision_required')
        executions.update(execution_id, remote_job=receipt['job_ref'], stages=[
            'parameters_validated', 'source_fingerprinted', 'admission_verified', 'remote_finished'])
    else:
        waiting = module.wait_for_job(record['remote_job'], timeout=900)
        if waiting['status'] not in ('passed', 'failed'):
            raise core.Refusal('Calcul distant non terminé ou inconnu ; aucune libération.', 'decision_required')
        receipt = None
    record = executions.get(execution_id, project)
    profile, remote_id = module.job_target(record['remote_job'])
    archive = temporary / 'fetch.tar'
    with archive.open('xb') as target:
        code, _ = module.transport(profile['ssh_host'], 'fetch', {'id': remote_id, 'paths': PATHS}, output=target, timeout=120)
    if code:
        raise core.Refusal('Collecte interrompue ; calcul préservé.', 'decision_required')
    fetched = temporary / 'fetched'
    module.unpack_result(archive, fetched)
    remote_receipt = json.loads((fetched / 'receipt.json').read_text())
    if (remote_receipt.get('id') != remote_id or remote_receipt.get('agent') != agent
            or remote_receipt.get('argv') != COMMAND or remote_receipt.get('status') not in ('passed', 'failed')):
        raise core.Refusal('Reçu distant incompatible ; aucune libération.', 'decision_required')
    report_path = fetched / 'work/output/task-tmp/validation.json'
    if not report_path.is_file():
        raise core.Refusal('Preuve validation.json absente ; aucune validation ni libération.', 'decision_required')
    report = json.loads(report_path.read_text())
    passed = (remote_receipt['status'] == 'passed' and remote_receipt.get('returncode') == 0
              and report.get('passed') is True and report.get('complete') is True
              and type(report.get('tests')) is int and report['tests'] > 0
              and report.get('failures') == 0 and report.get('errors') == 0)
    artifacts = project / 'output/routines-runs' / execution_id
    hashes = {}
    with core.directory_fd(artifacts, create=True) as directory:
        for relative in PATHS:
            source = fetched / relative
            if not source.is_file() or source.stat().st_size > core.MAX_INPUT:
                raise core.Refusal('Preuve attendue absente ou trop volumineuse : ' + relative, 'decision_required')
            name = source.name
            data = source.read_bytes()
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as out:
                    out.write(data)
            except FileExistsError:
                if core.read_bytes(project, str((artifacts / name).relative_to(project))) != data:
                    raise core.Refusal('Preuve précédente différente : ' + name + ' ; préservée.', 'decision_required')
            hashes[str(artifacts / name)] = hashlib.sha256(data).hexdigest()
            if hashlib.sha256(core.read_bytes(project, str((artifacts / name).relative_to(project)))).hexdigest() != hashes[str(artifacts / name)]:
                raise core.Refusal('Copie de preuve non vérifiée.', 'decision_required')
    current_source = source_signature(project, module)
    unchanged = current_source == record['source']
    status = 'ok' if passed and unchanged else 'decision_required' if not unchanged else 'error'
    result = {'id': 'checks.regression', 'execution_id': execution_id, 'status': status,
              'operation_state': 'completed', 'validation_status': 'passed' if passed and unchanged else 'stale' if not unchanged else 'failed',
              'result': {'summary': {'tests': report['tests'], 'source_unchanged': unchanged, 'remote_job': record['remote_job']}},
              'artifacts': hashes, 'evidence': [], 'duration_ms': round((time.time() - record['created_at']) * 1000, 2)}
    with core.evidence_dir(project) as (destination, directory):
        name = uuid.uuid4().hex + '.json'
        result['evidence'] = [str(destination / name)]
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'w') as out:
            json.dump(result, out)
    executions.finish(execution_id, result)
    executions.update(execution_id, artifacts=hashes, source=record['source'], stages=[
        'parameters_validated', 'source_fingerprinted', 'admission_verified', 'remote_finished', 'results_collected', 'sha256_verified', 'assertions_checked'])
    code, released = module.json_request(profile['ssh_host'], 'release', {'id': remote_id})
    if code or released.get('released') != remote_id:
        raise core.Refusal('Résultats conservés ; libération non confirmée.', 'decision_required')
    executions.update(execution_id, released=True, stages=[
        'parameters_validated', 'source_fingerprinted', 'admission_verified', 'remote_finished',
        'results_collected', 'sha256_verified', 'assertions_checked', 'remote_released'])
    print(json.dumps({'execution_id': execution_id, 'status': status, 'released': True}))


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '_bridge' or sys.argv[3] not in ('start', 'resume'):
        raise SystemExit('Usage interne uniquement.')
    try:
        bridge(executions.valid_id(sys.argv[2]), sys.argv[3])
    except Exception as error:
        executions.update(sys.argv[2], state='decision_required', owner_pid=None, decision=core.redact(str(error))[:220])
        print(json.dumps({'status': 'decision_required', 'message': core.redact(str(error))[:220]}))
        raise SystemExit(1)
