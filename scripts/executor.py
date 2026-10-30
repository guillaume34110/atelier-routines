"""Read-only routines shared by the CLI and MCP; no catalogue command is evaluated."""
import ast
from collections import Counter
import csv
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit
import wave
import zipfile

import routines

MAX_INPUT = 8 * 1024 * 1024
MAX_CAPTURE = 256 * 1024
MAX_EVIDENCE = 16 * 1024 * 1024
COMMANDS = 0
READS = {}
ERROR_INDEX = []
ERRORS_LIMITED = False
OWNED = {}
INTERRUPTED = False
EXCLUDED = {'.git', '.ssh', '.codex', '.aws', '.gnupg', 'node_modules', '.venv',
            '__pycache__', 'dist', 'output', 'outputs', '.pytest_cache', 'build', 'target', '.cache', 'coverage', '.next', '.nuxt'}


class Refusal(ValueError):
    def __init__(self, message, status='error'):
        super().__init__(message)
        self.status = status


def schema(**extra):
    fields = {'project': {'type': 'string', 'minLength': 1, 'maxLength': 2048}}
    fields.update(extra)
    return {'type': 'object', 'properties': fields,
            'required': [k for k, v in fields.items() if 'default' not in v],
            'additionalProperties': False}


FILE = {'type': 'string', 'minLength': 1, 'maxLength': 1024}
SEARCH = schema(pattern={'type': 'string', 'minLength': 1, 'maxLength': 200,
                         'description': 'Texte littéral.'},
                path={'type': 'string', 'minLength': 1, 'maxLength': 1024, 'default': '.'})
SCHEMAS = {
    'system.context': schema(), 'project.manifest': schema(), 'project.tree': schema(),
    'project.outline': schema(file=FILE), 'project.search': SEARCH, 'project.symbol': SEARCH,
    'git.status': schema(), 'git.diff': schema(), 'git.staged': schema(),
    'files.json': schema(file=FILE), 'files.logs': schema(file=FILE),
    'files.csv': schema(file=FILE, delimiter={'type': 'string', 'enum': [',', ';', '\t', '|'],
                                           'default': ','}),
}
OPTIONAL_PATH = {'type': 'string', 'minLength': 1, 'maxLength': 1024, 'default': '.'}
SCHEMAS.update({
    'project.instructions': schema(path=OPTIONAL_PATH),
    'project.errors': schema(file=FILE), 'checks.failure': schema(file=FILE),
    'checks.coverage': schema(file=FILE), 'files.archive': schema(file=FILE),
    'audio.inspect': schema(file=FILE),
    'git.log': schema(path=OPTIONAL_PATH, limit={'type': 'integer', 'minimum': 1, 'maximum': 30, 'default': 8}),
    'git.blame': schema(file=FILE, line={'type': 'integer', 'minimum': 1, 'maximum': 10000000},
                        count={'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 10}),
    'git.branches': schema(), 'git.remote': schema(),
    'system.preflight': schema(), 'system.memory': schema(),
    'system.storage': schema(gib={'type': 'number', 'exclusiveMinimum': 0, 'maximum': 100000}),
    'system.processes': schema(pid={'type': 'integer', 'minimum': 1, 'maximum': 2147483647}),
    'web.ports': schema(port={'type': 'integer', 'minimum': 1, 'maximum': 65535}),
    'web.api-health': schema(port={'type': 'integer', 'minimum': 1024, 'maximum': 65535},
                             path={'type': 'string', 'enum': ['/health', '/healthz', '/readyz'], 'default': '/health'}),
    'routines.register': schema(name={'type': 'string', 'maxLength': 48}, description={'type': 'string', 'maxLength': 120},
                                inputs={'type': 'object', 'default': {}},
                                steps={'type': 'array', 'minItems': 1, 'maxItems': 8}),
    'routines.list': schema(),
    'routines.manage': schema(name={'type': 'string', 'maxLength': 48},
                              action={'type': 'string', 'enum': ['show', 'enable', 'disable']}),
    'executions.inspect': schema(execution_id={'type': 'string', 'minLength': 32, 'maxLength': 32},
                                  detail={'type': 'string', 'enum': ['summary', 'errors', 'steps'], 'default': 'summary'},
                                  start={'type': 'integer', 'minimum': 0, 'maximum': 1000000, 'default': 0},
                                  limit={'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 5}),
    'executions.wait': schema(execution_id={'type': 'string', 'minLength': 32, 'maxLength': 32},
                               seconds={'type': 'integer', 'minimum': 1, 'maximum': 3600, 'default': 3600}),
    'executions.resume': schema(execution_id={'type': 'string', 'minLength': 32, 'maxLength': 32}),
    'executions.resolve': schema(execution_id={'type': 'string', 'minLength': 32, 'maxLength': 32},
                                 reason={'type': 'string', 'minLength': 1, 'maxLength': 200}),
    'reports.compare': schema(reference={'type': 'string', 'minLength': 32, 'maxLength': 32},
                              current={'type': 'string', 'minLength': 32, 'maxLength': 32}),
    'reports.validate': schema(file=FILE, assertions={'type': 'array', 'minItems': 1, 'maxItems': 16}),
    'checks.regression': schema(wait={'type': 'boolean', 'default': True}),
    'jobs.wait': schema(pid={'type': 'integer', 'minimum': 0, 'maximum': 2147483647, 'default': 0},
                        seconds={'type': 'integer', 'minimum': 1, 'maximum': 3600, 'default': 900}),
})


def storage_guard():
    path = Path('/app/storage-guard.py') if sys.platform.startswith('linux') else routines.BIN / 'storage-guard.py'
    if not path.is_file():
        path = Path(__file__).with_name('guards') / 'storage-guard.py'
    return path if path.is_file() else None


def lsof_path():
    return shutil.which('lsof') or ('/usr/sbin/lsof' if Path('/usr/sbin/lsof').is_file() else None)


def catalogue():
    import workflows
    return json.loads(routines.CATALOGUE.read_text()) + workflows.entries()


def availability(entry):
    if entry['id'].startswith('custom.'):
        import workflows
        return workflows.availability(entry)
    if entry['kind'] == 'proposal':
        return 'unavailable', ['Proposition exclue de la sélection.']
    if entry['id'] not in SCHEMAS:
        return 'guided', ['Appliquer la procédure avec les gardes et autorisations existants.']
    required = ['rtk']
    if entry['id'].startswith('git.') or entry['id'] == 'system.context':
        required.append('git')
    if entry['id'] in ('project.tree', 'project.search', 'project.symbol'):
        required.append('rg')
    if entry['id'] == 'system.processes':
        required.append('ps')
    if entry['id'] == 'jobs.wait':
        required.extend(('ps', 'pgrep'))
    missing = [name for name in required if not shutil.which(name)]
    if entry['id'] == 'web.ports' and not lsof_path():
        missing.append('lsof')
    if entry['id'] == 'system.storage' and not storage_guard():
        missing.append('garde canonique storage-guard.py')
    if entry['id'] == 'checks.regression':
        import vps_cycle
        if vps_cycle.client_path() is None:
            missing.append('client Atelier VPS installé sur cette machine')
    return ('unavailable', ['Commande absente : ' + ', '.join(missing)]) if missing else ('executable', [])


def parameters(entry):
    if entry['id'].startswith('custom.'):
        import workflows
        return workflows.parameters(entry)
    return SCHEMAS.get(entry['id'], {})


def find(query, limit=3):
    if type(query) is not str or not 1 <= len(query.strip()) <= 300:
        raise Refusal('query doit contenir entre 1 et 300 caractères.')
    if type(limit) is not int or not 1 <= limit <= 3:
        raise Refusal('limit doit être un entier entre 1 et 3.')
    found = []
    for entry in routines.match(catalogue(), query)[:limit]:
        state, needs = availability(entry)
        found.append({'id': entry['id'], 'description': entry['description'], 'availability': state,
                      'params': parameters(entry), 'prerequisites': needs})
    result = {'matches': found, 'executed': False}
    while len(json.dumps(result, ensure_ascii=False).encode()) > 1950 and len(found) > 1:
        result.setdefault('alternatives', []).insert(0, found.pop()['id'])
    return result


def validate(identifier, params):
    if type(params) is not dict:
        raise Refusal('params doit être un objet JSON.')
    if identifier.startswith('routines.'):
        import workflows
        return workflows.check_params(identifier, params)
    if identifier.startswith('custom.'):
        import workflows
        spec = workflows.parameters(workflows.entry(identifier))
    else:
        spec = SCHEMAS.get(identifier)
    if spec is None:
        if params:
            raise Refusal('Une procédure guidée ou exclue accepte uniquement params={}.')
        return {}
    if set(params) - spec['properties'].keys() or set(spec['required']) - params.keys():
        raise Refusal('Paramètres manquants ou inconnus ; consulter routines_find.')
    result = {}
    for name, rule in spec['properties'].items():
        value = params.get(name, rule.get('default'))
        validate_value(name, rule, value)
        result[name] = value
    return result


def validate_value(name, rule, value):
    if rule['type'] == 'string':
        valid = type(value) is str and rule.get('minLength', 0) <= len(value) <= rule.get('maxLength', 2048) and '\0' not in value
    elif rule['type'] == 'boolean':
        valid = type(value) is bool
    elif rule['type'] == 'array':
        valid = type(value) is list and rule.get('minItems', 0) <= len(value) <= rule.get('maxItems', 16)
    else:
        valid = type(value) in ((int,) if rule['type'] == 'integer' else (int, float))
        valid = valid and rule.get('minimum', -math.inf) <= value <= rule.get('maximum', math.inf)
        valid = valid and math.isfinite(value) and value > rule.get('exclusiveMinimum', -math.inf)
    if not valid or ('enum' in rule and value not in rule['enum']):
        raise Refusal('Valeur interdite : ' + name)


def forbidden(path):
    for part in Path(path).parts:
        name = part.casefold()
        if (name in {'.git', '.ssh', '.codex', '.aws', '.gnupg', '.npmrc', '.pypirc', '.netrc', '.git-credentials', '.s3cfg'} or name == 'claude.md' or name.startswith(('.env', 'id_rsa', 'id_ed25519'))
                or any(word in name for word in ('secret', 'credential', 'token'))
                or name.endswith(('.pem', '.key', '.p12', '.pfx'))):
            return True
    return False


def roots():
    raw = os.environ.get('ATELIER_ROUTINES_ROOTS')
    values = json.loads(raw) if raw else [str(Path(__file__).resolve().parents[2])]
    if not isinstance(values, list) or not values or any(not isinstance(p, str) or not Path(p).is_absolute() for p in values):
        raise Refusal('Configuration des racines invalide.')
    return [Path(p).resolve(strict=True) for p in values]


def project_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or forbidden(path):
        raise Refusal('Projet interdit ; fournir un dossier absolu autorisé.')
    resolved = path.resolve(strict=True)
    if len(os.fsencode(resolved)) > 1024 or not resolved.is_dir() or forbidden(resolved) or not any(resolved.is_relative_to(r) for r in roots()):
        raise Refusal('Projet hors des racines autorisées.')
    return resolved


def local_path(project, value):
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts or forbidden(relative):
        raise Refusal('Chemin relatif interdit.')
    path = project / relative
    current = project
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise Refusal('Les liens symboliques ne sont pas suivis.')
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(project) or forbidden(resolved):
        raise Refusal('Chemin hors projet ou protégé.')
    return resolved


@contextmanager
def directory_fd(path, create=False):
    """Open every component without following links, including intermediate parents."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular_file(project, name, limit=MAX_INPUT):
    path = local_path(project, name)
    with directory_fd(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise Refusal('Fichier non régulier ou trop volumineux (limite ' + str(limit) + ' octets).')
        yield source


def read_bytes(project, name, limit=MAX_INPUT):
    with regular_file(project, name, limit) as source:
        before = os.fstat(source.fileno())
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if len(data) > limit:
        raise Refusal('Le fichier a dépassé la limite pendant la lecture.')
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise Refusal('Entrée modifiée pendant la lecture ; aucune réutilisation.')
    if len(READS) < 64 or name in READS:
        READS[name] = {'file': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    return data


def read_file(project, name, limit=MAX_INPUT):
    data = read_bytes(project, name, limit)
    return data.decode('utf-8-sig')


def redact(text):
    text = re.sub(r'(?i)\b(authorization|password|passwd|secret|token|api[_-]?key)\b[\s"\x27:=]+\S+', r'\1=[REDACTED]', str(text))
    return re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]+)\b', '[REDACTED]', text)


def capture(argv, cwd, timeout=12, limit=MAX_CAPTURE, payload=None, group=False):
    """Bound bytes and lifetime, including the dispatcher and its children."""
    global COMMANDS
    COMMANDS += 1
    if argv[:2] != ['rtk', 'proxy']:
        raise Refusal('Commande interne non préfixée par rtk.')
    permitted = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TASK_WORKSPACE', 'PYTHONDONTWRITEBYTECODE'}
    env = {k: v for k, v in os.environ.items() if k in permitted or k.startswith(('ATELIER_ROUTINES_', 'USEVPS_'))}
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if payload else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, process_group=0 if group else None)
    if group:
        OWNED[process.pid] = process
    output = bytearray()
    limited = False
    deadline = time.monotonic() + timeout
    try:
        if payload:
            process.stdin.write(payload)
            process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Refusal('Délai dépassé ; processus de cette routine arrêtés.', 'timeout')
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(output) + len(chunk) > limit:
                        output.extend(chunk[:limit - len(output)])
                        limited = True
                        process.kill()
                        selector.unregister(key.fileobj)
                        break
                    else:
                        output.extend(chunk)
        process.wait(timeout=max(.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise Refusal('Délai dépassé ; processus de cette routine arrêtés.', 'timeout') from None
    finally:
        if group:
            # A new process group permits cleanup without detaching the job or touching existing services.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        OWNED.pop(process.pid, None)
    return process.returncode, output.decode('utf-8', errors='replace'), limited


def git(project, *args):
    if not (project / '.git').exists() or (project / '.git').is_symlink():
        raise Refusal('Fournir la racine du dépôt Git ou du worktree, avec une entrée .git régulière.')
    code, text, limited = capture(['rtk', 'proxy', 'git', '--no-optional-locks', '--no-pager',
                                 '-c', 'core.fsmonitor=false', '-c', 'core.pager=cat',
                                 '-c', 'log.showSignature=false', '-c', 'log.mailmap=false', *args], project)
    if code and not limited:
        raise Refusal('Git a échoué (code ' + str(code) + ') : ' + redact(text[:180]))
    return text, limited


def diff(project, staged=False):
    args = ['diff', '--no-ext-diff', '--no-textconv', '--numstat']
    if staged:
        args.append('--cached')
    text, limited = git(project, *args, '--')
    changes = []
    for line in text.splitlines():
        fields = line.split('\t', 2)
        if len(fields) == 3 and not forbidden(fields[2]):
            changes.append({'file': fields[2], 'added': int(fields[0]) if fields[0].isdigit() else None,
                            'deleted': int(fields[1]) if fields[1].isdigit() else None})
    return {'files': len(changes), 'added': sum(c['added'] or 0 for c in changes),
            'deleted': sum(c['deleted'] or 0 for c in changes)}, changes, limited


def rg(project, *args):
    argv = ['rtk', 'proxy', 'rg', '--no-config', '--no-ignore', '--hidden']
    for part in sorted(EXCLUDED):
        argv += ['-g', '!**/' + part + '/**']
    for pattern in ('CLAUDE.md', '**/CLAUDE.md', '**/.env*', '**/*secret*', '**/*credential*', '**/*token*',
                    '**/*.pem', '**/*.key', '**/*.p12', '**/*.pfx', '**/id_rsa*', '**/id_ed25519*'):
        argv += ['--iglob', '!' + pattern]
    code, text, limited = capture(argv + list(args), project)
    if code not in (0, 1) and not limited:
        raise Refusal('rg a échoué (code ' + str(code) + ') : ' + redact(text[:180]))
    return text, limited


def triage(project, name, tests=False):
    lines = read_file(project, name).splitlines()
    pattern = re.compile(r'AssertionError|^FAIL(?:ED|:)|^E\s{2,}|Exception|Error:|Caused by:' if tests
                         else r'\berror\b|\bexception\b|\bfatal\b|traceback|caused by:|\btimeout\b', re.I)
    index_errors(lines, pattern)
    hits, count = [], 0
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            count += 1
            if len(hits) < 30:
                hits.append({'line': i, 'text': redact(line)[:240]})
    return {'lines': len(lines), 'error_lines': count, 'first_error_line': hits[0]['line'] if hits else None,
            'checks_executed': False, 'scope': 'Extraits candidats ; aucune cause déduite ni relance.'}, hits, False


def index_errors(lines, pattern):
    global ERROR_INDEX, ERRORS_LIMITED
    indexed = {}
    for index, line in enumerate(lines, 1):
        if not pattern.search(line):
            continue
        digest = hashlib.sha256(line.encode()).hexdigest()
        if digest not in indexed:
            if len(indexed) >= 500:
                ERRORS_LIMITED = True
                continue
            indexed[digest] = {'digest': digest, 'count': 0, 'locations': [], 'preview': redact(line)[:80]}
        item = indexed[digest]
        item['count'] += 1
        if len(item['locations']) < 5:
            item['locations'].append(index)
    ERROR_INDEX = list(indexed.values())


def memory(project):
    if sys.platform.startswith('linux'):
        values = {}
        with open('/proc/meminfo') as source:
            for line in source:
                key, value = line.split(':', 1)
                if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                    values[key] = int(value.split()[0]) * 1024
        return values, []
    if sys.platform == 'darwin':
        details = []
        for args in (['memory_pressure', '-Q'], ['sysctl', 'vm.swapusage']):
            code, text, limited = capture(['rtk', 'proxy', *args], project)
            if code or limited:
                raise Refusal('Diagnostic mémoire macOS indisponible.')
            details.append(text.strip()[:600])
        return {'platform': 'darwin', 'admission_checked': False}, details
    raise Refusal('Diagnostic mémoire disponible sur Linux et macOS.', 'unavailable')


def extra_execute(identifier, project, params):
    details, partial = [], False
    if identifier in ('project.errors', 'checks.failure'):
        return triage(project, params['file'], identifier == 'checks.failure')
    if identifier == 'project.instructions':
        target = local_path(project, params['path'])
        target = target if target.is_dir() else target.parent
        root = max((r for r in roots() if project.is_relative_to(r)), key=lambda r: len(r.parts))
        chain = [p for p in [target, *target.parents] if p.is_relative_to(root)]
        for parent in reversed(chain):
            for name in ('AGENTS.md', 'AGENTS.override.md'):
                path = parent / name
                if path.exists() or path.is_symlink():
                    text = read_file(root, str(path.relative_to(root)), 256 * 1024)
                    details.append({'file': str(path.relative_to(root)), 'text': redact(text)})
        summary = {'instruction_files': len(details), 'scope': 'Parents et cible dans la racine autorisée ; instructions globales externes non incluses.'}
    elif identifier == 'git.log':
        text, partial = git(project, 'log', '--no-show-signature', '-n', str(params['limit']),
                            '--format=%H%x09%aI%x09%s', '--', params['path'])
        for line in text.splitlines():
            fields = line.split('\t', 2)
            if len(fields) == 3:
                details.append({'commit': fields[0], 'date': fields[1], 'subject': redact(fields[2])[:240]})
        summary = {'commits': len(details), 'scope': 'Historique local, sans réseau.'}
    elif identifier == 'git.blame':
        count = len(read_file(project, params['file']).splitlines())
        if params['line'] > count:
            raise Refusal('Ligne hors fichier.')
        end = min(count, params['line'] + params['count'] - 1)
        text, partial = git(project, 'blame', '--no-textconv', '--line-porcelain', '-L',
                            str(params['line']) + ',' + str(end), '--', params['file'])
        current = None
        for line in text.splitlines():
            match = re.fullmatch(r'([0-9a-f]{40,64}) (\d+) (\d+)(?: \d+)?', line)
            if match:
                current = {'commit': match[1], 'line': int(match[3])}
                details.append(current)
            elif current is not None and line.startswith('summary '):
                current['subject'] = redact(line[8:])[:180]
        summary = {'lines': len(details), 'scope': 'Origine locale, sans contenu de source ni identité d’auteur.'}
    elif identifier == 'git.branches':
        text, partial = git(project, 'for-each-ref', '--count=101', '--sort=refname',
                            '--format=%(refname:short)%09%(objectname:short)%09%(upstream:short)%09%(upstream:track)%09%(HEAD)', 'refs/heads')
        rows = text.splitlines()
        partial |= len(rows) > 100
        for row in rows[:100]:
            fields = row.split('\t')
            if len(fields) == 5:
                details.append(dict(zip(('branch', 'commit', 'upstream', 'divergence', 'active'), fields)))
        summary = {'branches': len(details), 'scope': 'Références locales ; aucun fetch, cible de PR non déduite.'}
    elif identifier == 'git.remote':
        text, partial = git(project, 'remote', '-v')
        seen = set()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0] in seen:
                continue
            seen.add(fields[0])
            raw = fields[1]
            parsed = urlsplit(raw)
            host, path, transport = parsed.hostname, parsed.path, parsed.scheme
            if not transport:
                scp = re.fullmatch(r'(?:[^@/]+@)?([\w.-]+):(.+)', raw)
                host, path, transport = (scp[1], scp[2], 'ssh') if scp else (None, '', 'local')
            repo = '/'.join(path.strip('/').split('/')[-2:])
            details.append({'name': fields[0], 'transport': transport, 'host': host,
                            'repository': redact(repo) if transport != 'local' else '[local path omitted]'})
        summary = {'remotes': len(details), 'credentials_omitted': True}
    elif identifier == 'files.archive':
        if Path(params['file']).suffix.casefold() != '.zip':
            raise Refusal('Inspection autonome ZIP seulement ; autres formats : procédure guidée.', 'unavailable')
        with zipfile.ZipFile(io.BytesIO(read_bytes(project, params['file']))) as archive:
            entries = archive.infolist()
            if len(entries) > 10000:
                raise Refusal('Archive limitée à 10 000 entrées.')
            names = set()
            compressed = expanded = encrypted = 0
            for entry in entries:
                name = entry.filename.replace('\\', '/')
                if (name.startswith('/') or '..' in Path(name).parts or re.match(r'^[A-Za-z]:', name)
                        or stat.S_ISLNK(entry.external_attr >> 16) or name in names):
                    raise Refusal('Archive refusée : traversée, lien ou nom dupliqué.')
                names.add(name)
                compressed += entry.compress_size
                expanded += entry.file_size
                encrypted += bool(entry.flag_bits & 1)
                details.append({'file': name, 'bytes': entry.file_size})
        summary = {'entries': len(entries), 'compressed_bytes': compressed, 'declared_bytes': expanded,
                   'encrypted_entries': encrypted, 'contents_verified': False, 'extraction_performed': False}
    elif identifier == 'checks.coverage':
        data = json.loads(read_file(project, params['file']))
        if not isinstance(data, dict):
            raise Refusal('Rapport de couverture JSON requis.')
        if isinstance(data.get('totals'), dict):
            totals = data['totals']
            covered, total = totals.get('covered_lines'), totals.get('num_statements')
        elif isinstance(data.get('total'), dict) and isinstance(data['total'].get('lines'), dict):
            covered, total = data['total']['lines'].get('covered'), data['total']['lines'].get('total')
        else:
            raise Refusal('Format reconnu : coverage.py JSON ou Istanbul coverage-summary JSON.')
        if type(covered) is not int or type(total) is not int or not 0 <= covered <= total:
            raise Refusal('Comptes de couverture incohérents.')
        summary = {'covered_lines': covered, 'total_lines': total, 'missing_lines': total - covered,
                   'percent': round(100 * covered / total, 2) if total else None,
                   'reported_only': True, 'checks_executed': False}
    elif identifier == 'audio.inspect':
        if Path(params['file']).suffix.casefold() != '.wav':
            raise Refusal('Inspection autonome PCM/WAV ≤8 Mio ; autres médias : procédure guidée.', 'unavailable')
        try:
            with wave.open(io.BytesIO(read_bytes(project, params['file'])), 'rb') as media:
                summary = {'format': 'PCM/WAVE', 'channels': media.getnchannels(), 'sample_rate': media.getframerate(),
                           'sample_width': media.getsampwidth(), 'frames': media.getnframes(),
                           'seconds': round(media.getnframes() / media.getframerate(), 6), 'listening_validated': False}
        except wave.Error as error:
            raise Refusal('WAV PCM non pris en charge : ' + str(error), 'unavailable') from None
    elif identifier in ('system.preflight', 'system.memory'):
        summary, details = memory(project)
        if identifier == 'system.preflight':
            disk = shutil.disk_usage(project)
            summary['disk_free_bytes'], summary['disk_total_bytes'] = disk.free, disk.total
            summary['cpu_count'] = os.cpu_count()
        summary['admission_checked'] = False
    elif identifier == 'system.storage':
        code, text, partial = capture(['rtk', 'proxy', sys.executable, str(storage_guard()),
                                       '--volume', str(project), '--require-gib', str(params['gib'])], project)
        if code or partial:
            raise Refusal('Garde stockage refusé : ' + redact(text[:220]))
        summary = {'admitted': True, 'requested_peak_gib': params['gib'], 'guard': 'storage-guard.py'}
        details = [redact(text[:1200])]
    elif identifier == 'system.processes':
        code, text, partial = capture(['rtk', 'proxy', 'ps', '-p', str(params['pid']),
                                       '-o', 'pid=,ppid=,uid=,etime=,comm='], project)
        if code:
            raise Refusal('Processus absent ou non accessible.', 'unavailable')
        fields = text.strip().split(None, 4)
        if len(fields) != 5 or int(fields[2]) != os.getuid():
            raise Refusal('Processus appartenant à un autre utilisateur.', 'unavailable')
        summary = dict(zip(('pid', 'parent_pid', 'uid', 'elapsed', 'executable'), fields))
        summary['arguments_omitted'] = True
    elif identifier == 'web.ports':
        code, text, partial = capture(['rtk', 'proxy', lsof_path(), '-nP', '-iTCP:' + str(params['port']),
                                       '-sTCP:LISTEN', '-Fpcu'], project)
        if code not in (0, 1) or (code == 1 and text.strip()):
            raise Refusal('Inspection des ports indisponible : ' + redact(text[:180]))
        current = None
        for line in text.splitlines():
            if line.startswith('p'):
                current = {'pid': line[1:]}
                details.append(current)
            elif current is not None and line[:1] in ('c', 'u'):
                current['command' if line[0] == 'c' else 'uid'] = line[1:]
        summary = {'port': params['port'], 'listeners': len(details), 'services_started': False}
    elif identifier == 'web.api-health':
        connection = http.client.HTTPConnection('127.0.0.1', params['port'], timeout=2)
        try:
            connection.request('GET', params['path'], headers={'Connection': 'close'})
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise Refusal('Santé API : HTTP ' + str(response.status) + ' ; aucune redirection suivie.')
            summary = {'host': '127.0.0.1', 'port': params['port'], 'http_status': response.status,
                       'healthy_endpoint': True, 'body_omitted': True, 'checks_executed': False,
                       'scope': 'Endpoint de santé seulement ; aucune assertion métier ou E2E.'}
        finally:
            connection.close()
    else:
        raise Refusal('Identifiant non implémenté.')
    return summary, details, partial


def execute(identifier, params):
    project = project_path(params['project'])
    if identifier.startswith(('custom.', 'routines.')):
        import workflows
        return workflows.execute(identifier, project, params)
    if identifier in ('reports.compare', 'reports.validate'):
        import executions
        result = executions.compare(project, params) if identifier == 'reports.compare' else executions.validate_report(project, params)
        result['inputs'] = list(READS.values())
        return result
    partial = False
    details = []
    if identifier == 'project.tree':
        text, partial = rg(project, '--files', '--', '.')
        lines = text.splitlines()
        if partial:
            lines = lines[:-1]
        files = [p.removeprefix('./') for p in lines if not forbidden(p)]
        summary = {'files': len(files), 'folders': dict(Counter(p.split('/')[0] if '/' in p else '(root)' for p in files).most_common(10)),
                   'extensions': dict(Counter(Path(p).suffix or '(none)' for p in files).most_common(10))}
        details = files
    elif identifier in ('system.context', 'project.manifest'):
        names = ['package.json', 'pyproject.toml', 'requirements.txt', 'Package.swift', 'Cargo.toml',
                 'package-lock.json', 'pnpm-lock.yaml', 'uv.lock']
        manifests = [n for n in names if (project / n).exists() and local_path(project, n).is_file()]
        scripts = []
        if 'package.json' in manifests:
            data = json.loads(read_file(project, 'package.json', 1024 * 1024))
            if not isinstance(data, dict) or not isinstance(data.get('scripts', {}), dict):
                raise Refusal('Structure package.json invalide.')
            scripts = list(data.get('scripts', {}))
        workflow_dir = project / '.github/workflows'
        workflows = []
        if workflow_dir.exists():
            checked = local_path(project, '.github/workflows')
            workflows = [str(p.relative_to(project)) for p in checked.iterdir()
                         if p.suffix in ('.yml', '.yaml') and not p.is_symlink()]
        summary = {'manifests': manifests, 'scripts': scripts[:20], 'workflows': workflows[:20],
                   'checks_executed': False}
        details = scripts + workflows
        if identifier == 'system.context' and (project / '.git').exists():
            text, partial = git(project, 'status', '--porcelain=v1', '--branch', '--untracked-files=normal')
            summary['git'] = [line for line in text.splitlines() if not forbidden(line[3:])][:20]
    elif identifier == 'project.outline':
        if Path(params['file']).suffix != '.py':
            raise Refusal('Un fichier .py est requis.')
        parsed = ast.parse(read_file(project, params['file'], 256 * 1024))
        details = [{'line': n.lineno, 'kind': type(n).__name__, 'name': n.name} for n in ast.walk(parsed)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        details.sort(key=lambda n: n['line'])
        summary = {'declarations': len(details)}
    elif identifier in ('project.search', 'project.symbol'):
        local_path(project, params['path'])
        if '\n' in params['pattern'] or '\r' in params['pattern']:
            raise Refusal('Recherche littérale sur une seule ligne.')
        text, partial = rg(project, '--files', '--', params['path'])
        names = text.splitlines()
        if partial:
            names = names[:-1]
        matches = scanned = volume = detail_bytes = skipped = 0
        for path in names[:5000]:
            if forbidden(path):
                continue
            checked = local_path(project, path)
            info = checked.lstat()
            if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                skipped += 1
                continue
            if volume + info.st_size > 32 * 1024 * 1024:
                partial = True
                break
            data = read_bytes(project, path, 2 * 1024 * 1024)
            volume += len(data)
            scanned += 1
            try:
                source = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                skipped += 1
                continue
            if '\0' in source:
                skipped += 1
                continue
            for index, line in enumerate(source.splitlines(), 1):
                count = line.count(params['pattern'])
                if count:
                    matches += count
                    item = {'file': path, 'line': index, 'text': redact(line)[:240]}
                    details.append(item)
                    detail_bytes += len(json.dumps(item).encode())
                    if detail_bytes > MAX_CAPTURE // 2:
                        partial = True
                        break
            if partial and detail_bytes > MAX_CAPTURE // 2:
                break
        partial |= len(names) > 5000
        summary = {'matches': matches, 'matching_lines': len(details),
                   'scanned_files': scanned, 'skipped_files': skipped,
                   'scope': 'UTF-8 littéral ; fichiers ≤2 Mio, scan ≤32 Mio/5000 fichiers, hors liens et chemins protégés.'}
    elif identifier == 'git.status':
        text, partial = git(project, 'status', '--porcelain=v1', '--branch', '--untracked-files=normal')
        lines = [line for line in text.splitlines() if not forbidden(line[3:])]
        unstaged, u_details, u_partial = diff(project)
        staged, s_details, s_partial = diff(project, True)
        summary = {'branch': next((s[3:] for s in lines if s.startswith('## ')), None),
                   'status_entries': sum(not s.startswith('## ') for s in lines),
                   'unstaged': unstaged, 'staged': staged, 'checks_executed': False}
        details = {'status': lines, 'unstaged': u_details, 'staged': s_details}
        partial |= u_partial or s_partial
    elif identifier in ('git.diff', 'git.staged'):
        summary, details, partial = diff(project, identifier == 'git.staged')
        summary['scope'] = 'Statistiques seulement ; aucun contenu de diff ni contrôle exécuté.'
    elif identifier == 'files.json':
        data = json.loads(read_file(project, params['file']))
        values = data.values() if isinstance(data, dict) else data if isinstance(data, list) else [data]
        summary = {'type': type(data).__name__, 'items': len(data) if isinstance(data, (dict, list)) else 1,
                   'value_types': dict(Counter(type(v).__name__ for v in values))}
        details = list(data) if isinstance(data, dict) else []
    elif identifier == 'files.csv':
        reader = csv.reader(io.StringIO(read_file(project, params['file'])), delimiter=params['delimiter'], strict=True)
        headers = next(reader, [])
        if not headers or len(headers) > 512 or len(set(headers)) != len(headers) or any(not h or len(h) > 128 for h in headers):
            raise Refusal('En-têtes CSV vides, dupliqués ou trop volumineux.')
        empty = [0] * len(headers)
        count = 0
        for row in reader:
            if len(row) != len(headers):
                raise Refusal('Largeur CSV incohérente à la ligne ' + str(reader.line_num))
            count += 1
            for i, value in enumerate(row):
                empty[i] += not value.strip()
        summary = {'rows': count, 'columns': len(headers), 'empty_cells': sum(empty)}
        details = [{'column': h, 'empty': empty[i]} for i, h in enumerate(headers)]
    elif identifier == 'files.logs':
        lines = read_file(project, params['file']).splitlines()
        categories = Counter()
        pattern = re.compile(r'\b(error|exception|fatal|traceback|timeout|warning|warn)\b', re.I)
        index_errors(lines, re.compile(r'\b(error|exception|fatal|traceback|timeout)\b', re.I))
        for index, line in enumerate(lines, 1):
            found = pattern.findall(line)
            if found:
                categories.update(s.casefold() for s in found)
                if len(details) < 1000:
                    details.append({'line': index, 'text': redact(line)[:240]})
        summary = {'lines': len(lines), 'matching_lines': sum(bool(pattern.search(line)) for line in lines),
                   'categories': dict(categories), 'excerpts_limited': len(details) == 1000}
    else:
        summary, details, partial = extra_execute(identifier, project, params)
    result = {'summary': summary, 'details': details, 'partial': partial, 'internal_commands': COMMANDS}
    result['inputs'] = list(READS.values())
    if identifier in ('files.logs', 'project.errors', 'checks.failure'):
        result['error_index'] = ERROR_INDEX
        result['errors_limited'] = ERRORS_LIMITED
    # shortcut: evidence is capped at 256 KiB; narrow the scope for larger inspections.
    while len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CAPTURE - 4096 and details:
        if isinstance(details, list):
            del details[len(details) // 2:]
        else:
            details.clear()
        result['details_limited'] = True
    return result


@contextmanager
def evidence_dir(project):
    path = project / 'output/routines-evidence'
    with directory_fd(path, create=True) as directory:
        mode = os.fstat(directory)
        if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise Refusal('Le dossier de preuves doit appartenir à l’utilisateur et être privé (0700).')
        lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise Refusal('Verrou de preuves non régulier.')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal('Une autre routine utilise les preuves de ce projet ; aucune attente ni relance automatique.') from None
            count = total = 0
            with os.scandir(directory) as files:
                for item in files:
                    if item.name == '.lock':
                        continue
                    info = item.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise Refusal('Entrée de preuves non régulière.')
                    count += 1
                    total += info.st_size
                    if count >= 1024 or total + MAX_CAPTURE > MAX_EVIDENCE:
                        raise Refusal('Budget de preuves atteint (16 Mio / 1024 reçus) ; préserver avant nettoyage autorisé.')
            yield path, directory
        finally:
            os.close(lock)


def run(identifier, params):
    started = time.monotonic()
    record = {'id': identifier if isinstance(identifier, str) and len(identifier) < 100 else 'unknown',
              'status': 'error', 'evidence': []}
    destination = None
    stack = ExitStack()
    execution_id = None
    launched = False
    try:
        if type(identifier) is not str:
            raise Refusal('id doit être un identifiant de routine.')
        entry = next((e for e in catalogue() if e['id'] == identifier), None)
        if entry is None:
            raise Refusal('Identifiant inconnu.')
        params = validate(identifier, params)
        state, needs = availability(entry)
        if state != 'executable':
            record.update(status=state, prerequisites=needs,
                          procedure={k: entry[k] for k in ('command', 'steps', 'boundary') if k in entry})
            return record
        project = project_path(params['project'])
        params['project'] = str(project)
        for key in ('file', 'path') if identifier != 'web.api-health' and not identifier.startswith(('custom.', 'routines.')) else ():
            if key in params:
                local_path(project, params[key])
        import executions
        if identifier == 'executions.inspect':
            return compact(executions.inspect(project, params))
        snapshot = None
        if identifier in executions.CACHEABLE:
            data = read_bytes(project, params['file'])
            snapshot = hashlib.sha256(data).hexdigest()
        execution_id, existing = executions.begin(identifier, params, snapshot)
        if existing is not None:
            existing['lookup_duration_ms'] = round((time.monotonic() - started) * 1000, 2)
            return existing
        record['execution_id'] = execution_id
        record['engine_version'] = executions.get(execution_id, project)['engine']
        record['operation_state'] = 'running'
        executions.update(execution_id, state='running', stages=['parameters_validated', 'operation_started'])
        destination, directory = stack.enter_context(evidence_dir(project))
        timeout = float(os.environ.get('ATELIER_ROUTINES_TIMEOUT', '15'))
        if not 0 < timeout <= 30:
            raise Refusal('Le délai configuré doit être >0 et ≤30 secondes.')
        launched = True
        code, text, limited = capture(['rtk', 'proxy', sys.executable, str(Path(__file__).resolve()), '_worker', identifier],
                                     project, timeout=timeout, payload=json.dumps(params).encode(), group=True)
        if limited:
            raise Refusal('Résultat interne trop volumineux ; réduire le périmètre.')
        if code:
            raise Refusal('Échec du processus interne (code ' + str(code) + ') : ' + redact(text[:180]))
        try:
            worker = json.loads(text)
        except json.JSONDecodeError:
            raise Refusal('Réponse du processus interne non JSON : ' + redact(text[:160])) from None
        if worker['status'] != 'ok':
            raise Refusal(worker['message'], worker['status'])
        result = worker['result']
        if identifier in executions.CACHEABLE:
            observed = next((item['sha256'] for item in result.get('inputs', []) if item['file'] == params['file']), None)
            current = hashlib.sha256(read_bytes(project, params['file'])).hexdigest()
            if observed != snapshot or current != snapshot:
                raise Refusal('Entrée modifiée pendant l’exécution ; résultat non réutilisable.', 'decision_required')
        record.update(status=result.get('workflow_status', 'partial' if result['partial'] else 'ok'), result=result,
                      internal_commands=result['internal_commands'], operation_state='completed',
                      validation_status=result.get('validation_status', 'not_requested'))
    except (Refusal, OSError, ValueError, TypeError) as error:
        state = getattr(error, 'status', 'error')
        record.update(status=state, message=redact(str(error))[:300],
                      operation_state='timeout' if state == 'timeout' else 'completed' if launched else 'refused')
    record['duration_ms'] = round((time.monotonic() - started) * 1000, 2)
    if INTERRUPTED and execution_id is not None:
        record.update(status='decision_required', operation_state='unknown',
                      decision='Serveur interrompu ; aucune relance automatique.')
    if destination is not None:
        path = destination / (uuid.uuid4().hex + '.json')
        record['evidence'] = [str(path)]
        try:
            with directory_fd(destination) as current:
                if os.fstat(current).st_ino != os.fstat(directory).st_ino or os.fstat(current).st_dev != os.fstat(directory).st_dev:
                    raise OSError('Le dossier de preuves a changé.')
            with os.fdopen(os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory), 'w') as out:
                json.dump(record, out, ensure_ascii=False)
        except OSError:
            return {'id': record['id'], 'status': 'error', 'message': 'Impossible de conserver le reçu.', 'evidence': []}
        finally:
            stack.close()
    if isinstance(identifier, str) and identifier.startswith('custom.') and record['evidence']:
        import workflows
        try:
            workflows.completed(identifier, record)
        except (Refusal, OSError, ValueError) as error:
            record.update(status='error', message='Exécution conservée ; suivi du registre indisponible : ' + redact(str(error))[:120])
    if execution_id is not None:
        try:
            executions.finish(execution_id, record)
        except (Refusal, OSError, ValueError) as error:
            record.update(status='decision_required', decision='Preuves conservées ; suivi incomplet : ' + redact(str(error))[:100])
    return compact(record)


def compact(record):
    encoded = json.dumps(record, ensure_ascii=False).encode()
    if len(encoded) > 1950:
        result = record.get('result', {})
        record['result'] = {'summary': result.get('summary', {}),
                            'preview': redact(json.dumps(result.get('details', []), ensure_ascii=False))[:500]}
        record['response_truncated'] = True
        if len(json.dumps(record, ensure_ascii=False).encode()) > 1950:
            record['result'] = {'preview': redact(json.dumps(result, ensure_ascii=False))[:800]}
    if len(json.dumps(record, ensure_ascii=False).encode()) > 1950:
        record.pop('result', None)
        for key in ('message', 'decision'):
            if isinstance(record.get(key), str):
                record[key] = record[key].encode()[:200].decode(errors='ignore')
        record['response_truncated'] = True
    if len(json.dumps(record, ensure_ascii=False).encode()) > 1950:
        keep = {'id', 'execution_id', 'status', 'operation_state', 'validation_status', 'evidence', 'response_truncated'}
        record = {key: value for key, value in record.items() if key in keep}
    return record


if __name__ == '__main__':
    # Registry and worker must share one module instance and command counter.
    sys.modules['executor'] = sys.modules[__name__]
    if len(sys.argv) != 3 or sys.argv[1] != '_worker' or (sys.argv[2] not in SCHEMAS and not sys.argv[2].startswith('custom.')):
        raise SystemExit('Usage interne seulement.')
    try:
        values = json.loads(sys.stdin.buffer.read(32769))
        result = execute(sys.argv[2], validate(sys.argv[2], values))
        response = {'status': 'ok', 'result': result}
    except (Refusal, OSError, ValueError, SyntaxError, TypeError, RecursionError, csv.Error, zipfile.BadZipFile,
            http.client.HTTPException, EOFError) as error:
        response = {'status': getattr(error, 'status', 'error'), 'message': redact(str(error))[:300]}
    print(json.dumps(response, ensure_ascii=False))
