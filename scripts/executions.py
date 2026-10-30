"""Durable execution IDs, verified file reuse and exact report comparisons."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

import executor as core
import workflows

CACHEABLE = {'files.json', 'files.csv', 'files.logs', 'files.archive', 'project.outline',
             'project.errors', 'checks.failure', 'checks.coverage', 'audio.inspect'}
ACTIVE = {'starting', 'running', 'collecting'}
REQUEST_ID = ContextVar('routine_request_id', default=None)


def root():
    path = workflows.root()
    return path.with_name(path.name + '-executions')


@contextmanager
def locked():
    with core.directory_fd(root(), create=True) as directory:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise core.Refusal('Le suivi doit être privé (0700) et appartenir à l’utilisateur.')
        lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise core.Refusal('Verrou de suivi non régulier.')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise core.Refusal('Suivi occupé ; aucune relance automatique.') from None
            total = count = 0
            with os.scandir(directory) as files:
                for item in files:
                    info = item.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise core.Refusal('Entrée de suivi non régulière.')
                    count += 1
                    total += info.st_size
                    if count > 1025 or total + workflows.MAX_RECIPE > 16 * 1024 * 1024:
                        raise core.Refusal('Budget de suivi atteint (1 024 exécutions / 16 Mio).')
            yield directory
        finally:
            os.close(lock)


def version():
    digest = hashlib.sha256(str(sys.version_info[:3]).encode())
    base = Path(__file__).resolve().parent
    for name in ('routines.py', 'executor.py', 'workflows.py', 'executions.py', 'vps_cycle.py', 'jobs.py', 'wait_hook.py'):
        path = base / name
        if path.exists():
            digest.update(path.read_bytes())
    digest.update(core.routines.CATALOGUE.read_bytes())
    lock = base.parent / 'requirements.lock'
    if lock.is_file():
        digest.update(lock.read_bytes())
    return digest.hexdigest()


def valid_id(identifier):
    if type(identifier) is not str or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise core.Refusal('execution_id doit contenir 32 caractères hexadécimaux.')
    return identifier


def get(identifier, project):
    valid_id(identifier)
    with locked() as directory:
        record = workflows.read(directory, identifier + '.json')
    if record.get('project') != str(project) or record.get('execution_id') != identifier:
        raise core.Refusal('Exécution hors du projet demandé.')
    return record


def update(identifier, **changes):
    valid_id(identifier)
    with locked() as directory:
        record = workflows.read(directory, identifier + '.json')
        record.update(changes)
        record['updated_at'] = time.time()
        workflows.write(directory, identifier + '.json', record)
    return record


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def view(record):
    state = record['state']
    if state in ACTIVE and not alive(record.get('owner_pid')):
        state = 'decision_required'
    return {'id': record['id'], 'execution_id': record['execution_id'], 'status': state,
            'operation_state': 'unknown' if state == 'decision_required' else state,
            'validation_status': record.get('validation_status', 'not_requested'),
            'result': record.get('summary', {}), 'stages': record.get('stages', []),
            'evidence': record.get('evidence', []), 'remote_job': record.get('remote_job'),
            'released': record.get('released', False), 'submission_attempts': record.get('submission_attempts', 0),
            'temporary_workspace': record.get('temporary_workspace'),
            'decision': record.get('decision') or ('Appel interrompu : inspecter ; ne pas relancer automatiquement.' if state == 'decision_required' else None)}


def verified_view(record):
    result = view(record)
    if record['state'] in ('ok', 'error', 'partial') and record.get('evidence'):
        try:
            proof(record)
        except (OSError, ValueError, core.Refusal) as error:
            result.update(status='decision_required', decision='Preuve absente ou altérée : ' + str(error)[:100])
    elif record['state'] == 'ok':
        result.update(status='decision_required', decision='Résultat sans preuve ; aucune validation acquise.')
    return result


def fingerprint(identifier, params, snapshot=None):
    encoded = json.dumps({'id': identifier, 'params': params, 'engine': version(), 'snapshot': snapshot},
                         sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def begin(identifier, params, snapshot=None):
    signature = fingerprint(identifier, params, snapshot)
    cached = None
    key = REQUEST_ID.get()
    key = hashlib.sha256(key.encode()).hexdigest() if key else None
    with locked() as directory:
        for name in os.listdir(directory):
            if not name.endswith('.json'):
                continue
            record = workflows.read(directory, name)
            if record['project'] != params['project']:
                continue
            if key is not None and key == record.get('request_key'):
                if record['signature'] != signature:
                    return None, {'id': identifier, 'execution_id': record['execution_id'], 'status': 'decision_required',
                                  'decision': 'Clé réutilisée avec des paramètres, entrées ou versions différents ; choisir une nouvelle demande.',
                                  'evidence': record.get('evidence', [])}
                result = verified_view(record)
                result.update(historical=True, facts_current=False)
                return None, result
            if record['state'] in ACTIVE | {'decision_required'}:
                if record['signature'] == signature:
                    return None, view(record)
                return None, {'id': identifier, 'status': 'decision_required', 'execution_id': record['execution_id'],
                              'evidence': record.get('evidence', []), 'decision': 'Une exécution utilise déjà ce projet ; consulter son suivi.'}
            if (identifier in CACHEABLE and record['signature'] == signature and record['state'] == 'ok'
                    and (key is None or record.get('request_key') is None)):
                cached = record
        if cached is None:
            execution_id = uuid.uuid4().hex
            scope = hashlib.sha256(json.dumps(params, sort_keys=True, allow_nan=False).encode()).hexdigest()
            record = {'execution_id': execution_id, 'id': identifier, 'project': params['project'], 'scope': scope,
                      'engine': version(), 'signature': signature, 'state': 'starting', 'owner_pid': os.getpid(),
                      'created_at': time.time(), 'stages': ['parameters_validated'], 'evidence': [], 'request_key': key}
            workflows.write(directory, execution_id + '.json', record)
            return execution_id, None
        if key is not None:
            cached['request_key'] = key
            workflows.write(directory, cached['execution_id'] + '.json', cached)
    try:
        receipt = proof(cached)
    except (OSError, ValueError, core.Refusal) as error:
        return None, {'id': identifier, 'execution_id': cached['execution_id'], 'status': 'decision_required',
                      'evidence': cached.get('evidence', []), 'decision': 'Preuve absente ou altérée ; aucun résultat réutilisé : ' + str(error)[:100]}
    receipt['reused'] = True
    receipt['source_internal_commands'] = receipt.get('internal_commands', 0)
    receipt['internal_commands'] = 0
    receipt['freshness_verified'] = True
    return None, core.compact(receipt)


def proof(record):
    paths = record.get('evidence', [])
    if not paths or len(paths) != len(record.get('proof_hashes', {})):
        raise core.Refusal('Preuves attendues manquantes.')
    first = None
    project = core.project_path(record['project'])
    for value in paths:
        path = Path(value)
        if not path.is_relative_to(project / 'output/routines-evidence') or not re.fullmatch(r'[a-f0-9]{32}\.json', path.name):
            raise core.Refusal('Chemin de reçu non autorisé.')
        data = core.read_bytes(project, str(path.relative_to(project)), core.MAX_CAPTURE)
        if hashlib.sha256(data).hexdigest() != record['proof_hashes'].get(value):
            raise core.Refusal('Empreinte de preuve différente.')
        if first is None:
            try:
                first = json.loads(data)
            except json.JSONDecodeError:
                raise core.Refusal('Reçu JSON invalide : ' + path.name + ' (' + str(len(data)) + ' octets).') from None
    if first is None or first.get('execution_id') != record['execution_id']:
        raise core.Refusal('Identité de preuve incohérente.')
    if record['id'] != 'checks.regression' and first.get('engine_version') != record['engine']:
        raise core.Refusal('Version de preuve incompatible.')
    for value, expected in record.get('artifacts', {}).items():
        path = Path(value)
        if path.parent != project / 'output/routines-runs' / record['execution_id'] or path.name not in (
                'receipt.json', 'stdout.log', 'validation.json', 'tests.log', 'benchmark.json'):
            raise core.Refusal('Chemin d’artefact interdit.')
        data = core.read_bytes(project, str(path.relative_to(project)))
        if hashlib.sha256(data).hexdigest() != expected:
            raise core.Refusal('Empreinte d’artefact différente.')
    return first


def finish(execution_id, result):
    hashes = {}
    with locked() as directory:
        record = workflows.read(directory, execution_id + '.json')
    project = core.project_path(record['project'])
    for value in result.get('evidence', []):
        path = Path(value)
        data = core.read_bytes(project, str(path.relative_to(project)), core.MAX_CAPTURE)
        hashes[value] = hashlib.sha256(data).hexdigest()
    summary = result.get('result', {}).get('summary', {})
    if len(json.dumps(summary).encode()) > 2000:
        summary = {'preview': json.dumps(summary, ensure_ascii=False)[:800], 'details_in_proof': True}
    return update(execution_id, state=result['status'], summary=summary,
                  validation_status=result.get('validation_status', 'not_requested'),
                  stages=['parameters_validated', 'operation_finished', 'evidence_verified'] if hashes else ['parameters_validated', 'operation_finished'],
                  evidence=result.get('evidence', []), proof_hashes=hashes,
                  message=result.get('message'), owner_pid=None)


def inspect(project, params):
    record = get(params['execution_id'], project)
    if params['detail'] == 'summary':
        return verified_view(record)
    receipt = proof(record)
    result = receipt.get('result', {})
    selected = result.get('error_index', []) if params['detail'] == 'errors' else result.get('details', [])
    if isinstance(selected, dict):
        selected = list(selected.items())
    start = params['start']
    return {'id': record['id'], 'execution_id': record['execution_id'], 'status': record['state'],
            'result': {'total_entries': len(selected), 'start': start,
                       'entries': selected[start:start + params['limit']]}, 'evidence': record['evidence']}


def resolve(project, params):
    record = get(params['execution_id'], project)
    if record['id'] == 'checks.regression' or record['state'] not in ACTIVE | {'decision_required'} or alive(record.get('owner_pid')):
        raise core.Refusal('Clôture réservée à une inspection locale incertaine dont le serveur propriétaire est arrêté.', 'decision_required')
    record = update(record['execution_id'], state='abandoned', decision=params['reason'], owner_pid=None,
                    stages=[*record.get('stages', []), 'unknown_explicitly_acknowledged'])
    return view(record)


def pointer(data, value):
    if type(value) is not str or len(value) > 256 or (value and not value.startswith('/')):
        raise core.Refusal('Pointeur JSON invalide.')
    parts = value.split('/')[1:] if value else []
    if len(parts) > 16:
        raise core.Refusal('Pointeur JSON limité à 16 segments.')
    for part in parts:
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(data, list):
            if not key.isdecimal() or int(key) >= len(data):
                raise KeyError(key)
            data = data[int(key)]
        elif isinstance(data, dict):
            data = data[key]
        else:
            raise KeyError(key)
    return data


def validate_report(project, params):
    assertions = params['assertions']
    if type(assertions) is not list or not 1 <= len(assertions) <= 16:
        raise core.Refusal('1 à 16 assertions explicites sont requises.')
    data = json.loads(core.read_file(project, params['file']))
    details, errors = [], []
    for assertion in assertions:
        if (type(assertion) is not dict or set(assertion) != {'pointer', 'equals'}
                or type(assertion['equals']) not in (str, int, float, bool, type(None))):
            raise core.Refusal('Assertion attendue : pointer et equals scalaire.')
        expected = assertion['equals']
        if len(json.dumps(expected, allow_nan=False)) > 1024:
            raise core.Refusal('Valeur d’assertion trop volumineuse.')
        try:
            actual = pointer(data, assertion['pointer'])
            passed = type(actual) is type(expected) and actual == expected
            observed = hashlib.sha256(json.dumps(actual, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (KeyError, IndexError):
            passed, observed = False, 'missing'
        details.append({'pointer': assertion['pointer'], 'passed': passed})
        if not passed:
            errors.append({'digest': hashlib.sha256((json.dumps(assertion, sort_keys=True) + observed).encode()).hexdigest(),
                           'count': 1, 'locations': [assertion['pointer']], 'preview': 'Assertion non satisfaite'})
    passed = all(item['passed'] for item in details)
    return {'summary': {'assertions': len(details), 'passed': sum(item['passed'] for item in details),
                        'validation_status': 'passed' if passed else 'failed', 'tests_executed': False},
            'details': details, 'error_index': errors, 'partial': False,
            'workflow_status': 'ok' if passed else 'error', 'validation_status': 'passed' if passed else 'failed',
            'internal_commands': 0}


def compare(project, params):
    reference, current = [get(params[name], project) for name in ('reference', 'current')]
    if (reference['engine'] != current['engine'] or current['engine'] != version()
            or reference['id'] != current['id'] or reference['scope'] != current['scope']):
        raise core.Refusal('Référence périmée ou périmètre incompatible ; choisir une référence compatible.', 'decision_required')
    before, after = proof(reference)['result'], proof(current)['result']
    if before.get('partial') or after.get('partial') or before.get('errors_limited') or after.get('errors_limited'):
        raise core.Refusal('Comparaison refusée : indices incomplets ; réduire le périmètre.', 'decision_required')
    if 'error_index' not in before or 'error_index' not in after:
        raise core.Refusal('Comparer des journaux ou rapports munis d’un index d’erreurs exact.', 'unavailable')
    old = {e['digest']: e for e in before['error_index']}
    new = {e['digest']: e for e in after['error_index']}
    details = []
    for digest in sorted(old.keys() | new.keys()):
        kind = 'persistent' if digest in old and digest in new else 'new' if digest in new else 'resolved'
        item = new.get(digest, old.get(digest))
        details.append({'kind': kind, 'digest': digest, 'preview': item['preview'],
                        'before_count': old.get(digest, {}).get('count', 0),
                        'after_count': new.get(digest, {}).get('count', 0), 'locations': item['locations']})
    summary = {kind: sum(d['kind'] == kind for d in details) for kind in ('new', 'persistent', 'resolved')}
    summary['explained'] = False
    return {'summary': summary, 'details': details, 'partial': False, 'internal_commands': 0}


async def dispatch(identifier, params, allow_background=True, request_id=None):
    import asyncio
    token = None
    try:
        if request_id is not None and (type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id)):
            raise core.Refusal('Clé de demande invalide (1 à 64 lettres, chiffres, tirets ou underscores).')
        token = REQUEST_ID.set(request_id)
        if identifier not in ('checks.regression', 'executions.wait', 'executions.resume', 'executions.resolve', 'jobs.wait'):
            return await asyncio.to_thread(core.run, identifier, params)
        params = core.validate(identifier, params)
        project = core.project_path(params['project'])
        if identifier == 'jobs.wait':
            import jobs
            params['project'] = str(project)
            return await jobs.start(project, params)
        if identifier == 'executions.resolve':
            return core.compact(resolve(project, params))
        import vps_cycle
        if identifier == 'checks.regression':
            if not allow_background and not params['wait']:
                raise core.Refusal('La CLI ne détache pas de processus ; wait=true ou MCP persistant requis.')
            return await vps_cycle.start(project, params)
        if identifier == 'executions.wait':
            if get(params['execution_id'], project)['id'] == 'jobs.wait':
                import jobs
                return await jobs.wait(project, params['execution_id'], params['seconds'])
            return await vps_cycle.wait(project, params['execution_id'], params['seconds'])
        return await vps_cycle.resume(project, params['execution_id'])
    except (core.Refusal, OSError, ValueError, TypeError) as error:
        return {'id': identifier, 'status': getattr(error, 'status', 'error'), 'operation_state': 'refused',
                'message': core.redact(str(error))[:220], 'evidence': []}
    finally:
        if token is not None:
            REQUEST_ID.reset(token)
