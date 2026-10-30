"""Wait on existing processes and supported tools; never starts or stops a job."""
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import sys
import threading
import time

SESSION_MS = 300000
CELL_MS = 900000
VPS_SECONDS = 3600
GUARDS = {Path.home() / '.local/bin/workspace-budget.py', Path(__file__).resolve().parent / 'guards/workspace-budget.py'}


class MonitorError(ValueError):
    pass


def command(argv, limit=16384):
    # Only fixed metadata commands reach this helper; never execute catalogue/user commands.
    process = subprocess.Popen(['rtk', 'proxy', *argv], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    data = bytearray()
    deadline = time.monotonic() + 5
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MonitorError('Diagnostic de processus expiré ; calcul préservé.')
            if not selector.select(remaining):
                raise MonitorError('Diagnostic de processus expiré ; calcul préservé.')
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                return process.wait(timeout=remaining), data.decode(errors='replace')
            data.extend(chunk)
            if len(data) > limit:
                raise MonitorError('Diagnostic de processus trop volumineux ; aucun résultat supposé complet.')
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()  # Own short metadata reader only; never the observed process.
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def identity(pid):
    code, text = command(['ps', '-p', str(pid), '-o', 'uid=,lstart=,stat='])
    if code == 1 and not text.strip():
        return None
    fields = text.split()
    if code or len(fields) != 7 or not fields[0].isdigit():
        raise MonitorError('Identité de processus non accessible ; calcul préservé.')
    return {'pid': pid, 'uid': int(fields[0]), 'started': ' '.join(fields[1:6]), 'state': fields[6]}


def working_directory(pid):
    if sys.platform.startswith('linux'):
        return Path(os.readlink(f'/proc/{pid}/cwd')).resolve()
    tool = '/usr/sbin/lsof' if Path('/usr/sbin/lsof').is_file() else 'lsof'
    code, text = command([tool, '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], 4096)
    names = [line[1:] for line in text.splitlines() if line.startswith('n')]
    if code or len(names) != 1:
        raise MonitorError('Dossier du processus non accessible ; calcul préservé.')
    return Path(names[0]).resolve()


def find_process(project, pid=0):
    if pid:
        found = identity(pid)
        if (found is None or found['uid'] != os.getuid() or pid == os.getpid()
                or not working_directory(pid).is_relative_to(project)):
            raise MonitorError('Processus absent, autre propriétaire ou hors projet.')
        return found
    code, text = command(['pgrep', '-u', str(os.getuid()), '-f', r'(^|/)workspace-budget[.]py( |$)'], 4096)
    if code == 1:
        raise MonitorError('Aucun garde local actif identifié ; fournir le PID connu ou utiliser le backend existant.')
    pids = text.split()
    if code or len(pids) > 32 or not all(p.isdigit() for p in pids):
        raise MonitorError('Recherche de gardes incomplète ; fournir un PID connu.')
    matches = []
    for value in pids:
        candidate = int(value)
        code, raw = command(['ps', '-p', value, '-o', 'command='])
        if code:
            continue
        try:
            # shortcut: ps loses argument quoting; supply a PID for a guard using a spaced absolute project argument.
            words = shlex.split(raw)
            if not words or not Path(words[0]).name.lower().startswith('python'):
                continue
            words = words[1:]
            while words and words[0] in ('-B', '-u', '-I'):
                words = words[1:]
            if not words or Path(words[0]).resolve() not in {p.resolve() for p in GUARDS}:
                continue
            run = words.index('run', 1)
            target = words[run + 1]
            cwd = working_directory(candidate)
            if (cwd / target).resolve() != project:
                continue
            found = identity(candidate)
            if found and found['uid'] == os.getuid():
                matches.append(found)
        except (ValueError, IndexError, ProcessLookupError, FileNotFoundError):
            continue
    if len(matches) != 1:
        raise MonitorError('Garde local absent ou ambigu ; fournir un PID connu.')
    return matches[0]


def wait_process(process, seconds, stop=None):
    stop = stop or threading.Event()
    started = time.monotonic()
    probes = 0
    while not stop.is_set():
        current = identity(process['pid'])
        probes += 1
        state = 'running'
        if current is None or current['state'].startswith('Z'):
            state = 'partial'
        elif any(current[k] != process[k] for k in ('pid', 'uid', 'started')):
            state = 'decision_required'
        elapsed = time.monotonic() - started
        if state != 'running' or elapsed >= seconds:
            return {'status': state, 'pid': process['pid'], 'waited_seconds': round(elapsed, 2),
                    'internal_probes': probes, 'exit_code_known': False, 'tests_validated': False}
        stop.wait(min(5, seconds - elapsed))
    raise MonitorError('Attente interrompue ; processus et référence préservés.')


def poll_project(args, data, roots):
    raw = args.get('command', args.get('cmd', ''))
    if not isinstance(raw, str) or len(raw) > 8192 or any(c in raw for c in (';', '&', '`', '$', '>', '<', '\n')):
        return None
    words = shlex.split(raw)
    if words[:2] == ['rtk', 'proxy']:
        words = words[2:]
    elif words[:1] == ['rtk']:
        words = words[1:]
    if not words or Path(words[0]).name not in ('tail', 'head', 'rg'):
        return None
    project = Path(args.get('workdir', args.get('cwd', data.get('cwd', '.')))).resolve()
    if not any(project.is_relative_to(root) for root in roots):
        return None
    log = project / 'output/task-tmp/stdout.log'
    return project if any((project / word).resolve() == log for word in words[1:] if not word.startswith('-')) else None


def number(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def response(data, roots=()):
    event = data.get('hook_event_name')
    name = data.get('tool_name', '').removeprefix('functions.')
    args = data.get('tool_input')
    if not isinstance(args, dict):
        return None
    if event == 'PreToolUse':
        if name in ('Bash', 'exec_command'):
            project = poll_project(args, data, roots)
            if project is not None:
                try:
                    process = find_process(project)
                except MonitorError:
                    return None
                result = wait_process(process, 900)
                if result['status'] != 'partial':
                    return {'hookEventName': event, 'permissionDecision': 'deny',
                            'permissionDecisionReason': 'Surveillance bornée terminée sans validation : '
                            + json.dumps(result) + '. Conserver le calcul ; aucune relance automatique.'}
                return {'hookEventName': event, 'additionalContext':
                        'Le moniteur a attendu la fin du processus connu ; code de sortie et artefacts restent à vérifier. '
                        + json.dumps(result)}
        updated = dict(args)
        if (name == 'wait' and isinstance(args.get('cell_id'), str) and args['cell_id']
              and not args.get('terminate', False) and number(args.get('yield_time_ms', 0), CELL_MS)):
            updated['yield_time_ms'] = CELL_MS
        elif (re.fullmatch(r'mcp__atelier[-_]vps__vps_wait', name)
              and isinstance(args.get('job_id'), str) and args['job_id']
              and number(args.get('timeout', VPS_SECONDS), VPS_SECONDS) and args.get('timeout', VPS_SECONDS) > 0):
            updated['timeout'] = VPS_SECONDS
        elif (re.fullmatch(r'mcp__atelier[-_]routines__routines_run', name)
              and args.get('id') == 'executions.wait' and isinstance(args.get('params'), dict)
              and type(args['params'].get('seconds', VPS_SECONDS)) is int
              and 1 <= args['params'].get('seconds', VPS_SECONDS) <= VPS_SECONDS):
            updated['params'] = {**args['params'], 'seconds': VPS_SECONDS}
        if updated != args:
            return {'hookEventName': event, 'permissionDecision': 'allow', 'updatedInput': updated}
        return None
    if event != 'PostToolUse':
        return None
    result = data.get('tool_response')
    if isinstance(result, dict) and isinstance(result.get('structuredContent'), dict):
        result = result['structuredContent']
    elif isinstance(result, dict) and isinstance(result.get('content'), list):
        blocks = result['content']
        if len(blocks) == 1 and blocks[0].get('type') == 'text':
            result = blocks[0].get('text')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {'output': result}
    if not isinstance(result, dict):
        return None
    message = None
    if name in ('Bash', 'exec_command') and result.get('exit_code') is None:
        session = result.get('session_id')
        if type(session) is not int:
            match = re.search(r'Process running with session ID (\d+)', str(result.get('output', '')))
            session = int(match[1]) if match else None
        if type(session) is int and session > 0:
            message = (f'Calcul local actif, session {session} : appeler jobs.wait sur le projet '
                       '(PID connu ou garde Atelier détectable), seconds=900. '
                       'Ne pas lire les logs ni commenter les compteurs entre deux attentes. '
                       'Si le code mode rend une cellule active, attendre cette même cellule avec '
                       'wait(yield_time_ms=900000). Aucun nouveau lancement. '
                       'À la fin, vérifier le code de sortie et les artefacts requis.')
    if (re.fullmatch(r'mcp__atelier[-_]routines__routines_run', name)
            and result.get('status') == 'running' and result.get('execution_id')):
        message = ('Calcul suivi actif : appeler directement executions.wait avec le même execution_id '
                   'et seconds=3600, sans sondage ni commentaire de progression. '
                   'À son échéance, conserver la référence et signaler la limite ; ne pas resoumettre.')
        if args.get('id') in ('executions.wait', 'jobs.wait'):
            message = ('Échéance de l’attente atteinte : calcul encore actif, aucune validation acquise. '
                       'Conserver execution_id et les preuves ; signaler cette limite sans relance automatique.')
    if (re.fullmatch(r'mcp__atelier[-_]vps__vps_(run|wait)', name)
            and result.get('status', result.get('state')) in ('running', 'queued') and result.get('job_id')):
        message = ('Calcul VPS actif : utiliser vps_wait sur le job_id connu avec timeout=3600. '
                   'Ne pas sonder vps_status ni commenter les compteurs. Garder la référence ; ne pas resoumettre.')
        if name.endswith('__vps_wait'):
            message = ('Échéance VPS atteinte : conserver le job_id, signaler l’état actif non validé ; '
                       'aucune relance ni libération automatique.')
    if message:
        return {'hookEventName': event, 'additionalContext': message}
    return None


def main():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        return
    try:
        data = json.loads(raw)
        roots = [Path(p).resolve() for p in json.loads(sys.argv[1])] if len(sys.argv) == 2 else []
        output = response(data, roots) if isinstance(data, dict) else None
    except (ValueError, TypeError, AttributeError, OverflowError, OSError, subprocess.SubprocessError):
        return
    if output:
        print(json.dumps({'hookSpecificOutput': output}, ensure_ascii=False))


if __name__ == '__main__':
    main()
