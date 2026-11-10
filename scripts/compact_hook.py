"""Prepare a recoverable evidence handoff around Codex's native compaction."""
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

from decision_payload import summarize, validate
from decision_runtime import api, private_json, read_endpoint
import usage

SOURCE = 'local-compaction'
OPERATION = 'context.compact'
MAX_AGE = 24 * 3600
METRICS = ('matches', 'returned', 'lines', 'errors', 'tests', 'pages', 'evidence_found', 'usable')


def projects(cwd, roots):
    path = Path(cwd)
    if not path.is_absolute():
        return []
    path = path.resolve(strict=True)
    if not path.is_dir():
        return []
    return [str(parent) for parent in (path, *path.parents) if any(parent.is_relative_to(root) for root in roots)]


def execution(directory, identifier):
    for parent in (directory, directory / 'history'):
        try:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            return private_json(parent / (identifier + '.json'))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
    return None


def candidates(connection, session, scope, now):
    journal = usage.root()
    directory = journal.with_name(journal.name.removesuffix('-usage') + '-executions')
    rows = connection.execute('''SELECT data FROM events WHERE session=? AND phase='end' AND source='hook'
        AND at>=? ORDER BY seq DESC LIMIT 256''', (session, now - MAX_AGE)).fetchall()
    seen, completed, critical = set(), [], []
    for raw, in rows:
        identifier = json.loads(raw).get('execution_id')
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier) or identifier in seen:
            continue
        if len(seen) == 32:
            break
        seen.add(identifier)
        record = execution(directory, identifier)
        if (not isinstance(record, dict) or record.get('execution_id') != identifier
                or record.get('project') not in scope or record.get('id') == 'usage.savings'):
            continue
        state = usage.name(record.get('state'))
        validation = usage.name(record.get('validation_status') or 'not_requested')
        row = {'execution_id': identifier, 'project': scope[record['project']],
               'operation': usage.name(record.get('id')), 'state': state, 'validation': validation}
        if state != 'ok' or validation != 'not_requested' or record.get('effect_uncertain'):
            if len(critical) < 3:
                critical.append(row)
        elif len(completed) < 4 and record.get('proof_hashes'):
            summary = record.get('summary', {})
            if isinstance(summary, dict):
                row['metrics'] = {key: summary[key] for key in METRICS if type(summary.get(key)) is bool or
                                  type(summary.get(key)) is int and 0 <= summary[key] <= 10**9}
            completed.append(row)
        if len(completed) == 4 and len(critical) == 3:
            break
    return critical, completed


def select(goal, completed):
    state = {'task': goal or 'Preserve evidence needed to resume the current coding task.', 'receipts': {}}
    questions = {}
    for i, row in enumerate(completed, 1):
        label = f'receipt_{i}'
        state['receipts'][label] = {k: row[k] for k in ('operation', 'metrics') if k in row}
        questions[label] = {'type': 'noul', 'instructions': f'Should {label} be referenced in the next short handoff? '
                            'Answer no only if it is clearly irrelevant or redundant for continuing the task.'}
    request = validate({'state': usage.encoded(state).decode(), 'questions': questions})
    endpoint = read_endpoint()
    started = time.monotonic()
    raw = api(endpoint['port'], endpoint['token'], request, timeout=2)
    answers = summarize(request, raw, .99, 'mac_metal', (time.monotonic() - started) * 1000)['answers']
    selected = [row for row, answer in zip(completed, answers.values()) if not answer['accepted'] or answer['value']]
    return selected, {'decisions': len(answers), 'accepted': sum(answer['accepted'] for answer in answers.values())}


def prepare(connection, session, turn, scope, trigger, now):
    critical, completed = candidates(connection, session, scope, now)
    prompt = connection.execute("SELECT data FROM events WHERE session=? AND phase='prompt' AND source='hook' "
                                'AND at>=? ORDER BY seq DESC LIMIT 1', (session, now - MAX_AGE)).fetchone()
    goal = json.loads(prompt[0]).get('instruction') if prompt else None
    data = {'trigger': trigger, 'delivery': 'prepared', 'cwd': next(iter(scope.values())), 'candidates': len(completed),
            'protected': len(critical), 'attempted': False, 'references': critical + completed}
    status = 'no_candidates'
    if completed and os.environ.get('ATELIER_ROUTINES_DECISION') != 'off':
        data['attempted'] = True
        try:
            selected, data['d1'] = select(goal, completed)
            data['references'] = critical + selected
            status = 'ok' if data['d1']['accepted'] == len(completed) else 'partial'
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
            status = 'unavailable'
    elif completed:
        status = 'disabled'
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        usage.reserve_space(connection)
        connection.execute('''INSERT INTO events(at,source,phase,session,turn,operation,status,data)
            VALUES (?,?,'end',?,?,?,?,?)''', (now, SOURCE, session, turn, OPERATION, status, usage.encoded(data).decode()))


def followup(connection, session, turn, scope, event, trigger, now):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        last = connection.execute('''SELECT seq,turn,data FROM events WHERE session=? AND phase='end' AND source=?
            AND at>=? ORDER BY seq DESC LIMIT 1''', (session, SOURCE, now - MAX_AGE)).fetchone()
        if last is None:
            return None
        seq, prepared_turn, raw = last
        data = json.loads(raw)
        if data.get('cwd') != next(iter(scope.values())):
            return None
        if event == 'PostCompact':
            if prepared_turn != turn or data.get('delivery') != 'prepared' or data.get('trigger') != trigger:
                return None
            data['delivery'] = 'ready'
        else:
            if data.get('delivery') != 'ready':
                return None
            data['delivery'] = 'delivered'
        connection.execute('UPDATE events SET data=? WHERE seq=?', (usage.encoded(data).decode(), seq))
    if event != 'SessionStart' or not data['references']:
        return None
    note = 'Atelier après compaction. Reçus historiques. Consulter executions.inspect avant de reprendre.'
    project_paths = {key: path for path, key in scope.items()}
    for row in data['references']:
        if row['project'] not in project_paths:
            continue
        line = f"\n{row['operation']} {row['state']} {row['execution_id']}"
        if row['validation'] != 'not_requested':
            line += ' validation=' + row['validation']
        if row['project'] != data['cwd']:
            line += ' project=' + json.dumps(project_paths[row['project']], ensure_ascii=False)
        if len((note + line).encode()) > 1100:
            break
        note += line
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': note}}


def run(data, roots):
    event, trigger, source = (data.get(key) for key in ('hook_event_name', 'trigger', 'source'))
    if event in ('PreCompact', 'PostCompact'):
        if trigger not in ('auto', 'manual') or not isinstance(data.get('turn_id'), str) or not data['turn_id']:
            return None
    elif event != 'SessionStart' or source != 'compact':
        return None
    if os.environ.get('ATELIER_ROUTINES_USAGE') == 'off':
        return None
    session_id, cwd = data.get('session_id'), data.get('cwd')
    if not isinstance(session_id, str) or not 0 < len(session_id) <= 128 or not isinstance(cwd, str):
        return None
    allowed = projects(cwd, roots)
    if not allowed:
        return None
    with usage.database(create=False) as connection:
        session = usage.digest(connection, session_id)
        turn = usage.digest(connection, [session, data.get('turn_id')])
        scope = {path: usage.digest(connection, path) for path in allowed}
        now = time.time()
        if event == 'PreCompact':
            prepare(connection, session, turn, scope, trigger, now)
            return None
        return followup(connection, session, turn, scope, event, trigger, now)


def main():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        return
    try:
        data = json.loads(raw)
        roots = [Path(root).resolve(strict=True) for root in json.loads(sys.argv[1])]
        output = run(data, roots) if isinstance(data, dict) else None
        if output:
            print(json.dumps(output, ensure_ascii=False))
    except (OSError, ValueError, TypeError, AttributeError, IndexError, KeyError, sqlite3.Error):
        return


if __name__ == '__main__':
    main()
