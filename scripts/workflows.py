"""Private, bounded recipes composed only from the fixed inspection allowlist."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

import executor as core

MANAGEMENT = {'routines.register', 'routines.list', 'routines.manage'}
NON_COMPOSABLE = MANAGEMENT | {'checks.regression', 'executions.inspect', 'executions.wait', 'executions.resume', 'executions.resolve', 'jobs.wait'}
MAX_STORE = 1024 * 1024
MAX_RECIPE = 16 * 1024
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,47}')


def root():
    value = Path(os.environ.get('ATELIER_ROUTINES_STORE', str(Path.home() / '.local/share/atelier-routines')))
    if not value.is_absolute() or '..' in value.parts:
        raise core.Refusal('Dossier de routines invalide.')
    return value


@contextmanager
def store(create=False):
    with core.directory_fd(root(), create=create) as directory:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise core.Refusal('Le registre doit appartenir à l’utilisateur et être privé (0700).')
        lock = os.open('.lock', os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise core.Refusal('Verrou de registre invalide.')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise core.Refusal('Registre occupé ; aucune attente ni relance automatique.') from None
            total = count = 0
            with os.scandir(directory) as files:
                for item in files:
                    info = item.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise core.Refusal('Entrée de registre non régulière.')
                    total += info.st_size
                    count += 1
                    if count > 202 or total + MAX_RECIPE > MAX_STORE:
                        raise core.Refusal('Budget du registre atteint (100 routines / 1 Mio).')
            yield directory
        finally:
            os.close(lock)


def read(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECIPE or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise core.Refusal('Fichier de routine non privé, non régulier ou trop volumineux.')
        data = source.read(MAX_RECIPE + 1)
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise core.Refusal('Métadonnées JSON invalides : ' + name + ' (' + str(len(data)) + ' octets).') from None


def write(directory, name, data):
    encoded = json.dumps(data, ensure_ascii=False).encode()
    if len(encoded) > MAX_RECIPE:
        raise core.Refusal('Routine limitée à 16 Kio.')
    temporary = '.new-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def check_name(value):
    if type(value) is not str or not NAME.fullmatch(value):
        raise core.Refusal('Nom attendu : 1 à 48 lettres minuscules, chiffres ou tirets.')
    return value


def definition(values):
    if type(values) is not dict or set(values) - {'name', 'description', 'inputs', 'steps'} or not {'name', 'description', 'steps'} <= values.keys():
        raise core.Refusal('Définition : name, description, inputs facultatifs et steps uniquement.')
    name = check_name(values['name'])
    description = values['description']
    if type(description) is not str or not 1 <= len(description) <= 120:
        raise core.Refusal('Description requise, 120 caractères maximum.')
    inputs, steps = values.get('inputs', {}), values['steps']
    if type(inputs) is not dict or len(inputs) > 8 or type(steps) is not list or not 1 <= len(steps) <= 8:
        raise core.Refusal('Au plus 8 paramètres et 1 à 8 étapes.')
    for key, rule in inputs.items():
        check_name(key)
        if key == 'project' or type(rule) is not dict or set(rule) - {'type', 'default'} or rule.get('type') not in ('string', 'integer', 'number'):
            raise core.Refusal('Paramètres simples uniquement ; project est réservé.')
        if 'default' in rule:
            validate_input(rule, rule['default'])
    for step in steps:
        if type(step) is not dict or set(step) != {'id', 'params'} or type(step['id']) is not str or step['id'] not in core.SCHEMAS or step['id'] in NON_COMPOSABLE:
            raise core.Refusal('Étape limitée à une inspection autorisée ; ni récursion ni gestionnaire.')
        params = step['params']
        spec = core.SCHEMAS[step['id']]
        if type(params) is not dict or 'project' in params or set(params) - spec['properties'].keys() or set(spec['required']) - {'project'} - params.keys():
            raise core.Refusal('Paramètres d’étape manquants, inconnus ou projet remplacé.')
        for key, value in params.items():
            rule = spec['properties'][key]
            if type(value) is dict:
                if set(value) != {'$input'} or type(value['$input']) is not str or value['$input'] not in inputs:
                    raise core.Refusal('Liaison de paramètre invalide.')
                declared = inputs[value['$input']]['type']
                if declared != rule['type'] and not (declared == 'integer' and rule['type'] == 'number'):
                    raise core.Refusal('Type de liaison incompatible.')
                if 'default' in inputs[value['$input']]:
                    core.validate_value(key, rule, inputs[value['$input']]['default'])
            else:
                core.validate_value(key, rule, value)
    result = {'name': name, 'description': description, 'inputs': inputs, 'steps': steps}
    if len(json.dumps(result).encode()) > MAX_RECIPE:
        raise core.Refusal('Routine limitée à 16 Kio.')
    return result


def validate_input(rule, value):
    core.validate_value('input', {'type': rule['type'], 'minLength': 0, 'maxLength': 1024,
                                  'minimum': -10000000, 'maximum': 10000000}, value)


def load(name, directory):
    result = definition(read(directory, name + '.json'))
    if result['name'] != name:
        raise core.Refusal('Nom de routine incohérent.')
    try:
        state = read(directory, '.state-' + name + '.json')
    except FileNotFoundError:
        state = {'enabled': True}
    if type(state) is not dict or type(state.get('enabled')) is not bool:
        raise core.Refusal('État de routine invalide.')
    return result, state


def entries():
    if not root().exists():
        return []
    result = []
    with store() as directory:
        names = sorted(n[:-5] for n in os.listdir(directory) if n.endswith('.json') and not n.startswith('.'))
        if len(names) > 100:
            raise core.Refusal('Registre limité à 100 routines.')
        for name in names:
            check_name(name)
            recipe, state = load(name, directory)
            result.append({'id': 'custom.' + name, 'kind': 'recipe', 'description': recipe['description'],
                           'triggers': [name, recipe['description']], 'recipe': recipe, 'state': state})
    return result


def entry(identifier):
    name = check_name(identifier.removeprefix('custom.'))
    with store() as directory:
        recipe, state = load(name, directory)
    return {'id': 'custom.' + name, 'kind': 'recipe', 'description': recipe['description'], 'recipe': recipe, 'state': state}


def parameters(item):
    fields = {}
    for key, rule in item['recipe']['inputs'].items():
        fields[key] = {'type': rule['type'], 'maxLength': 1024} if rule['type'] == 'string' else {
            'type': rule['type'], 'minimum': -10000000, 'maximum': 10000000}
        if 'default' in rule:
            fields[key]['default'] = rule['default']
    return core.schema(**fields)


def availability(item):
    if not item['state']['enabled']:
        return 'unavailable', ['Routine désactivée ; activer explicitement avec routines.manage.']
    for step in item['recipe']['steps']:
        state, needs = core.availability({'id': step['id'], 'kind': 'command'})
        if state != 'executable':
            return 'unavailable', needs
    return 'executable', []


def check_params(identifier, params):
    if type(params) is not dict or type(params.get('project')) is not str:
        raise core.Refusal('project est requis.')
    if identifier == 'routines.register':
        recipe = definition({k: v for k, v in params.items() if k != 'project'})
        return {'project': params['project'], **recipe}
    if identifier == 'routines.list':
        if set(params) != {'project'}:
            raise core.Refusal('routines.list accepte project uniquement.')
    else:
        if set(params) != {'project', 'name', 'action'} or params['action'] not in ('show', 'enable', 'disable'):
            raise core.Refusal('Gestion : project, name et action show/enable/disable.')
        check_name(params['name'])
    return params


def execute(identifier, project, params):
    if identifier == 'routines.list':
        listed = entries()
        return {'summary': {'routines': len(listed)}, 'details': [
            {'id': e['id'], 'description': e['description'], 'enabled': e['state']['enabled'],
             'last_run': e['state'].get('last_run')} for e in listed], 'partial': False, 'internal_commands': 0}
    if identifier in MANAGEMENT:
        name = params['name']
        with store(create=True) as directory:
            if identifier == 'routines.register':
                recipe = definition({k: v for k, v in params.items() if k != 'project'})
                try:
                    previous, state = load(name, directory)
                except FileNotFoundError:
                    previous, state = None, {'enabled': True}
                if previous is not None and previous != recipe:
                    raise core.Refusal('Nom déjà enregistré ; choisir un nouveau nom, aucune définition remplacée.')
                if previous is None:
                    if len([n for n in os.listdir(directory) if n.endswith('.json') and not n.startswith('.')]) >= 100:
                        raise core.Refusal('Registre limité à 100 routines.')
                    write(directory, name + '.json', recipe)
            else:
                recipe, state = load(name, directory)
                if params['action'] != 'show':
                    state['enabled'] = params['action'] == 'enable'
                    write(directory, '.state-' + name + '.json', state)
        return {'summary': {'id': 'custom.' + name, 'enabled': state['enabled'], 'steps': len(recipe['steps']),
                            'executed': False, 'last_run': state.get('last_run')},
                'details': [recipe] if params.get('action') == 'show' else [], 'partial': False, 'internal_commands': 0}
    item = entry(identifier)
    if not item['state']['enabled']:
        raise core.Refusal('Routine désactivée.', 'unavailable')
    results, state = [], 'ok'
    started_commands = core.COMMANDS
    for index, step in enumerate(item['recipe']['steps'], 1):
        try:
            values = {key: params[value['$input']] if type(value) is dict else value for key, value in step['params'].items()}
            values['project'] = str(project)
            values = core.validate(step['id'], values)
            for key in ('file', 'path') if step['id'] != 'web.api-health' else ():
                if key in values:
                    core.local_path(project, values[key])
            result = core.execute(step['id'], values)
            state = 'partial' if result['partial'] else 'ok'
            results.append({'step': index, 'id': step['id'], 'status': state, 'summary': result['summary']})
        except (core.Refusal, OSError, ValueError, TypeError, SyntaxError) as error:
            state = getattr(error, 'status', 'error')
            results.append({'step': index, 'id': step['id'], 'status': state, 'message': core.redact(str(error))[:220]})
        if state != 'ok':
            break
    return {'summary': {'id': identifier, 'steps_completed': sum(s['status'] == 'ok' for s in results),
                        'steps_total': len(item['recipe']['steps']), 'status': state},
            'details': results, 'workflow_status': state, 'partial': state == 'partial',
            'internal_commands': core.COMMANDS - started_commands}


def completed(identifier, record):
    if not identifier.startswith('custom.'):
        return
    name = check_name(identifier[7:])
    with store() as directory:
        _, state = load(name, directory)
        state['last_run'] = {'status': record['status'], 'duration_ms': record['duration_ms'],
                             'evidence': record['evidence'], 'timestamp': int(time.time())}
        write(directory, '.state-' + name + '.json', state)
