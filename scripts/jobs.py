"""Persistent, read-only waits on identified local computations; no job submission."""
import asyncio
import hashlib
import json
import os
import subprocess
import threading
import uuid

import executor as core
import executions
import wait_hook


async def start(project, params):
    execution_id, existing = executions.begin('jobs.wait', params)
    if existing is not None:
        return core.compact(existing)
    try:
        process = await asyncio.to_thread(wait_hook.find_process, project, params['pid'])
    except (wait_hook.MonitorError, OSError, subprocess.SubprocessError) as error:
        executions.update(execution_id, state='unavailable', owner_pid=None, decision=str(error))
        return core.compact(executions.view(executions.get(execution_id, project)))
    executions.update(execution_id, state='running', local_process=process,
                      stages=['parameters_validated', 'process_identified'])
    return await wait(project, execution_id, params['seconds'])


async def wait(project, execution_id, seconds):
    record = executions.get(execution_id, project)
    if record['id'] != 'jobs.wait' or not record.get('local_process'):
        raise core.Refusal('Référence de surveillance locale requise.')
    if record['state'] == 'partial':
        result = executions.verified_view(record)
        result.update(historical=True, facts_current=False)
        return core.compact(result)
    stop = threading.Event()
    try:
        summary = await asyncio.to_thread(wait_hook.wait_process, record['local_process'], seconds, stop)
        status = summary['status']
        if status == 'partial':
            summary['next_action'] = 'Collecter le code de sortie de la session connue et vérifier les artefacts ; aucun test validé.'
        elif status == 'running':
            summary['next_action'] = 'Échéance atteinte ; garder execution_id, calcul actif non validé. Aucune nouvelle soumission.'
        else:
            summary['next_action'] = 'Identité du PID remplacée ; ne pas suivre le nouveau processus.'
        proof = {'id': 'jobs.wait', 'execution_id': execution_id, 'engine_version': record['engine'],
                 'status': status, 'result': {'summary': summary, 'details': []}, 'validation_status': 'not_requested'}
        encoded = json.dumps(proof, ensure_ascii=False).encode()
        with core.evidence_dir(project) as (directory, fd):
            name = uuid.uuid4().hex + '.json'
            handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(handle, 'wb') as out:
                out.write(encoded)
            path = str(directory / name)
        executions.update(execution_id, state=status, summary=summary, validation_status='not_requested',
                          evidence=[path], proof_hashes={path: hashlib.sha256(encoded).hexdigest()},
                          stages=['parameters_validated', 'process_identified', 'bounded_wait_finished', 'evidence_verified'],
                          owner_pid=os.getpid() if status == 'running' else None)
        return core.compact(executions.verified_view(executions.get(execution_id, project)))
    except asyncio.CancelledError:
        executions.update(execution_id, state='decision_required', owner_pid=None,
                          decision='Surveillance interrompue ou inaccessible ; processus et référence préservés.')
        raise
    except (wait_hook.MonitorError, OSError, ValueError):
        executions.update(execution_id, state='decision_required', owner_pid=None,
                          decision='Surveillance inaccessible ; processus et référence préservés.')
        return core.compact(executions.view(executions.get(execution_id, project)))
    finally:
        stop.set()
