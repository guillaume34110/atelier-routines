"""Local, bounded observations; no model, raw commands, tool output or execution."""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid

CAP_BYTES = 64 * 1024 * 1024
APPLICATION_ID = 0x41545255
MAX_HOOK_BYTES = 512 * 1024
ANALYSIS_LIMIT = 2000
TOKEN_KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
              'output_tokens', 'reasoning_output_tokens', 'total_tokens')
TOKEN_SCAN_BYTES = 8 * 1024 * 1024
TOKEN_LINE_BYTES = 1024 * 1024


def root():
    store = Path(os.environ.get('ATELIER_ROUTINES_STORE', str(Path.home() / '.local/share/atelier-routines')))
    if not store.is_absolute() or '..' in store.parts:
        raise ValueError('Invalid usage directory')
    return store.with_name(store.name + '-usage')


@contextmanager
def database(create=True):
    directory = root()
    if directory.is_symlink():
        raise ValueError('Symbolic usage directory refused')
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Usage directory must be private and owned')
    path = directory / 'events.sqlite3'
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Usage database must be a private regular file')
    finally:
        os.close(fd)
    connection = sqlite3.connect(path, timeout=2)
    try:
        connection.execute('PRAGMA cache_size=-512')
        connection.execute('PRAGMA max_page_count=' + str(CAP_BYTES // connection.execute('PRAGMA page_size').fetchone()[0]))
        application = connection.execute('PRAGMA application_id').fetchone()[0]
        if application == 0:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                application = connection.execute('PRAGMA application_id').fetchone()[0]
                if application == 0:
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise ValueError('Foreign usage schema refused')
                    connection.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                    connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    connection.executemany('INSERT INTO meta VALUES (?, ?)', [('schema_version', '1'), ('salt', uuid.uuid4().hex)])
                    connection.execute('''CREATE TABLE events (
                seq INTEGER PRIMARY KEY, at REAL NOT NULL, source TEXT NOT NULL, phase TEXT NOT NULL,
                session TEXT, turn TEXT, call_id TEXT, operation TEXT NOT NULL, signature TEXT,
                status TEXT, data TEXT NOT NULL, event_key TEXT UNIQUE)''')
                    connection.execute('CREATE INDEX session_events ON events(session, phase, seq)')
                    connection.execute('CREATE INDEX call_events ON events(source, call_id, phase)')
                    application = APPLICATION_ID
        if application != APPLICATION_ID:
            raise ValueError('Foreign usage database refused')
        version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version is None:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                columns = [row[1] for row in connection.execute('PRAGMA table_info(events)')]
                salt = connection.execute("SELECT value FROM meta WHERE key='salt'").fetchone()
                if columns != 'seq at source phase session turn call_id operation signature status data event_key'.split() or not salt or not re.fullmatch('[a-f0-9]{32}', salt[0]):
                    raise ValueError('Unsupported legacy usage schema')
                connection.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('schema_version', '1'))
            version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version != ('1',):
            raise ValueError('Unsupported usage schema')
        yield connection
    except sqlite3.Error:
        raise ValueError('Usage database unavailable; data preserved') from None
    finally:
        connection.close()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def digest(connection, value):
    if value is None:
        return None
    salt = connection.execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
    return hmac.new(salt.encode(), encoded(value), hashlib.sha256).hexdigest()[:32]


def instruction(text):
    if not isinstance(text, str):
        return None
    text = re.sub(r'```.*?(?:```|$)', '[CODE]', text, flags=re.S)
    text = re.sub(r'-----BEGIN .*?(?:-----END [^-]+-----|$)', '[KEY]', text, flags=re.S)
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+\S+', '[AUTH]', text)
    text = re.sub(r'''(?ix)\b(?:authorization|password|passwd|secret|token|api[_-]?key)\b[\s"':=]+(?:"[^"]*"|'[^']*'|\S+)''', '[SECRET]', text)
    text = re.sub(r'\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|github_pat_\w+|AKIA[A-Z0-9]{16})\b', '[SECRET]', text)
    text = re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:/[^\s]+)+', '[PRIVATE]', text)
    text = re.sub(r'`[^`]*`', '[CODE]', text)
    return ' '.join(text.split())[:280]


def receipt(value):
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get('structuredContent'), dict):
        return value['structuredContent']
    blocks = value.get('content')
    if isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict):
        try:
            parsed = json.loads(blocks[0].get('text', ''))
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return value


def name(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}', value) else 'unknown'


def record(source, phase, operation, inputs=None, result=None, session=None, call_id=None, prompt=None, duration=None, turn_id=None):
    if os.environ.get('ATELIER_ROUTINES_USAGE') == 'off':
        return
    try:
        with database() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            session = digest(connection, session) if session else None
            call_id = digest(connection, [session, call_id]) if call_id else None
            previous = connection.execute("SELECT turn FROM events WHERE session=? AND phase='prompt' ORDER BY seq DESC LIMIT 1", (session,)).fetchone()
            if phase == 'end' and call_id and not turn_id:
                previous = connection.execute("SELECT turn FROM events WHERE source=? AND call_id=? AND phase='start' ORDER BY seq LIMIT 1", (source, call_id)).fetchone() or previous
            turn = digest(connection, [session, turn_id]) if turn_id else uuid.uuid4().hex if phase == 'prompt' else previous[0] if previous else None
            data = {}
            body = receipt(result)
            for key in ('validation_status', 'operation_state', 'status'):
                if isinstance(body.get(key), str):
                    data[key] = name(body[key])
            for key in ('reused', 'facts_current', 'response_truncated'):
                if type(body.get(key)) is bool:
                    data[key] = body[key]
            if isinstance(body.get('execution_id'), str) and re.fullmatch('[a-f0-9]{32}', body['execution_id']):
                data['execution_id'] = body['execution_id']
            if prompt is not None:
                data['instruction'] = instruction(prompt)
            if inputs is not None:
                data['input_bytes'] = len(encoded(inputs))
            if result is not None:
                data['output_bytes'] = len(encoded(result))
            if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
                data['duration_ms'] = round(duration * 1000, 3)
            usage = body.get('usage')
            if isinstance(usage, dict):
                data['reported_usage'] = {k: usage[k] for k in ('input_tokens', 'output_tokens', 'total_tokens') if type(usage.get(k)) is int and usage[k] >= 0}
            status = data.get('status', 'error' if body.get('isError') is True else None)
            event_key = digest(connection, [source, phase, call_id]) if call_id else None
            connection.execute('INSERT OR IGNORE INTO events(at,source,phase,session,turn,call_id,operation,signature,status,data,event_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                               (time.time(), source, phase, session, turn, call_id, name(operation), digest(connection, inputs), status, encoded(data).decode(), event_key))
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # Collection failure must neither hide an operation's result nor restart it.
        print('Atelier usage: collecte indisponible ; données potentiellement incomplètes.', file=sys.stderr)


@contextmanager
def capture(source, operation, inputs):
    call_id, started, observed = uuid.uuid4().hex, time.monotonic(), {}
    record(source, 'start', operation, inputs=inputs, call_id=call_id)
    try:
        yield observed
    except BaseException as error:
        status = 'ok' if isinstance(error, SystemExit) and error.code in (None, 0) else 'interrupted_or_error'
        record(source, 'end', operation, inputs=inputs, result={'status': status}, call_id=call_id, duration=time.monotonic() - started)
        raise
    else:
        record(source, 'end', operation, inputs=inputs, result=observed, call_id=call_id, duration=time.monotonic() - started)


def operation(tool, inputs):
    if tool in ('routines_run', 'mcp__atelier_routines__routines_run', 'mcp__atelier-routines__routines_run'):
        return name(inputs.get('id')) if isinstance(inputs, dict) else 'unknown'
    return name(tool)


def token_schema(connection):
    version = connection.execute("SELECT value FROM meta WHERE key='token_schema_version'").fetchone()
    if version is None:
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''CREATE TABLE IF NOT EXISTS token_cursors (
                source TEXT PRIMARY KEY, identity TEXT NOT NULL, offset INTEGER NOT NULL,
                size INTEGER NOT NULL, state TEXT NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL)''')
            connection.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('token_schema_version', '1'))
        version = connection.execute("SELECT value FROM meta WHERE key='token_schema_version'").fetchone()
    if version != ('1',):
        raise ValueError('Unsupported token schema')


@contextmanager
def token_file(value):
    base = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
    path = Path(value)
    relative = path.relative_to(base)
    if not base.is_absolute() or '..' in relative.parts or path.suffix != '.jsonl':
        raise ValueError('Unsupported token source')
    directory = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('Invalid token source')
            yield stream, info, relative.as_posix()
    finally:
        os.close(directory)


def token_counts(value):
    if not isinstance(value, dict):
        raise ValueError('Missing token counters')
    counts = {key: value.get(key) for key in TOKEN_KEYS}
    required = [key for key in TOKEN_KEYS if key != 'cache_write_input_tokens']
    if any(type(counts[k]) is not int or not 0 <= counts[k] <= 2**40 for k in required):
        raise ValueError('Invalid token counters')
    write = counts['cache_write_input_tokens']
    if write is not None and (type(write) is not int or not 0 <= write <= counts['input_tokens']):
        raise ValueError('Invalid cache write counter')
    if (counts['input_tokens'] + counts['output_tokens'] != counts['total_tokens']
            or counts['cached_input_tokens'] > counts['input_tokens']
            or counts['reasoning_output_tokens'] > counts['output_tokens']):
        raise ValueError('Inconsistent token counters')
    return counts


def token_event(connection, phase, state, data, stamp, key, label='unknown'):
    connection.execute('''INSERT OR IGNORE INTO events
        (at,source,phase,session,turn,operation,status,data,event_key) VALUES (?,?,?,?,?,?,?,?,?)''',
        (stamp, 'codex', phase, state.get('session'), state.get('turn'), label, 'measured',
         encoded(data).decode(), digest(connection, ['codex', key])))


def collect_tokens(path):
    if os.environ.get('ATELIER_ROUTINES_USAGE') == 'off':
        return {'status': 'disabled'}
    try:
        with token_file(path) as (stream, info, relative), database() as connection:
            token_schema(connection)
            source = digest(connection, relative)
            identity = str(info.st_dev) + ':' + str(info.st_ino)
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                info = os.fstat(stream.fileno())
                old = connection.execute('SELECT identity,offset,state FROM token_cursors WHERE source=?', (source,)).fetchone()
                offset, state = (old[1], json.loads(old[2])) if old else (0, {})
                prefix = state.get('prefix')
                changed = prefix and hashlib.sha256(stream.read(prefix[0])).hexdigest() != prefix[1]
                if old and (old[0] != identity or offset > info.st_size or changed):
                    connection.execute("UPDATE token_cursors SET status='source_changed' WHERE source=?", (source,))
                    return {'status': 'source_changed'}
                stream.seek(offset)
                added, consumed = 0, 0
                while stream.tell() < info.st_size and consumed < TOKEN_SCAN_BYTES:
                    position = stream.tell()
                    line = stream.readline(min(TOKEN_LINE_BYTES, TOKEN_SCAN_BYTES - consumed, info.st_size - position))
                    consumed += len(line)
                    if state.get('skipping'):
                        state['skipping'] = not line.endswith(b'\n')
                        continue
                    if not line.endswith(b'\n'):
                        if len(line) == TOKEN_LINE_BYTES:
                            state['skipping'] = True
                            state['gaps'] = state.get('gaps', 0) + 1
                            continue
                        stream.seek(position)
                        break
                    if not any(word in line for word in (b'"session_meta"', b'"turn_context"', b'"task_started"',
                            b'"task_complete"', b'"turn_aborted"', b'"token_count"', b'"function_call"', b'"custom_tool_call"')):
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get('payload', {})
                        category, kind = event.get('type'), payload.get('type')
                        stamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                        if category == 'session_meta':
                            if not isinstance(payload.get('id'), str):
                                raise ValueError('Missing session identity')
                            session = digest(connection, payload['id'])
                            if state.get('session') not in (None, session):
                                raise ValueError('Session identity changed')
                            state['session'] = session
                        elif category == 'turn_context' or category == 'event_msg' and kind == 'task_started':
                            if not state.get('session') or not isinstance(payload.get('turn_id'), str):
                                raise ValueError('Missing turn identity')
                            turn = digest(connection, [state['session'], payload['turn_id']])
                            if turn != state.get('turn'):
                                state.update(turn=turn, pending=[], pending_count=0)
                            token_event(connection, 'turn_start', state, {}, stamp, [state['session'], turn, 'start'])
                        elif category == 'event_msg' and kind in ('task_complete', 'turn_aborted'):
                            turn = digest(connection, [state.get('session'), payload['turn_id']]) if isinstance(payload.get('turn_id'), str) else state.get('turn')
                            token_event(connection, 'turn_end', {**state, 'turn': turn}, {'kind': kind}, stamp, [state.get('session'), turn, 'end'])
                        elif category == 'response_item' and kind in ('function_call', 'custom_tool_call'):
                            state['pending_count'] = state.get('pending_count', 0) + 1
                            pending = state.setdefault('pending', [])
                            if len(pending) < 32:
                                tool = name(payload.get('name'))
                                if tool in ('routines_run', 'mcp__atelier_routines__routines_run', 'mcp__atelier-routines__routines_run'):
                                    try:
                                        tool = operation(tool, json.loads(payload.get('arguments', '{}')))
                                    except (ValueError, TypeError):
                                        tool = 'unknown'
                                pending.append({'operation': tool,
                                    'call_id': digest(connection, [state.get('session'), payload.get('call_id')])})
                        elif category == 'event_msg' and kind == 'token_count':
                            usage = payload.get('info')
                            if not isinstance(usage, dict):
                                continue
                            if not state.get('session'):
                                raise ValueError('Missing session identity')
                            current = token_counts(usage.get('total_token_usage'))
                            previous = state.get('previous')
                            if current == previous:
                                continue
                            if not current['total_tokens'] or previous and current['total_tokens'] == previous['total_tokens']:
                                if previous and current != previous:
                                    state['gaps'] = state.get('gaps', 0) + 1
                                state['previous'] = current
                                continue
                            last = token_counts(usage.get('last_token_usage'))
                            counts = {k: current[k] - previous[k] if current[k] is not None and previous[k] is not None else None
                                      for k in TOKEN_KEYS} if previous else last
                            if previous:
                                try:
                                    counts = token_counts(counts)
                                except ValueError:
                                    state.update(previous=current, previous_turn=state.get('turn'), pending=[], pending_count=0)
                                    state['gaps'] = state.get('gaps', 0) + 1
                                    continue
                            elif current != last:
                                state['gaps'] = state.get('gaps', 0) + 1
                            if not counts['total_tokens']:
                                state.update(previous=current, previous_turn=state.get('turn'), pending=[], pending_count=0)
                                continue
                            attribution = 'single_inference' if counts == last else 'aggregate_interval'
                            owner = state.get('turn')
                            if not state.get('session') or attribution == 'aggregate_interval' and state.get('previous_turn') != owner:
                                owner = None
                            pending = state.get('pending', [])
                            labels = sorted({item['operation'] for item in pending})
                            count = state.get('pending_count', 0)
                            label = labels[0] if count == 1 and len(labels) == 1 else 'tool_batch' if count else 'model_only'
                            data = {'reported_usage': counts, 'provider_cumulative': current,
                                    'attribution': attribution, 'operations': labels,
                                    'call_ids': [item['call_id'] for item in pending], 'tool_calls': count,
                                    'tools_truncated': count > len(pending), 'source': source, 'offset': position}
                            before = connection.total_changes
                            token_event(connection, 'tokens', {**state, 'turn': owner}, data, stamp,
                                        [state.get('session'), owner, current], label)
                            added += connection.total_changes - before
                            state.update(previous=current, previous_turn=state.get('turn'), pending=[], pending_count=0)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        state['gaps'] = state.get('gaps', 0) + 1
                position = stream.tell()
                status = 'observed' if position == info.st_size and not state.get('gaps') and not state.get('skipping') else 'partial'
                stream.seek(0)
                prefix = stream.read(min(1024, info.st_size))
                state['prefix'] = [len(prefix), hashlib.sha256(prefix).hexdigest()]
                connection.execute('INSERT OR REPLACE INTO token_cursors VALUES (?,?,?,?,?,?,?)',
                    (source, identity, position, info.st_size, encoded(state).decode(), status, time.time()))
                return {'status': status, 'new_steps': added, 'bytes_read': consumed,
                        'caught_up': position == info.st_size, 'gaps': state.get('gaps', 0)}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {'status': 'unavailable', 'reason': 'source_or_storage_error'}


def current_token_source():
    try:
        identifier = uuid.UUID(os.environ.get('CODEX_THREAD_ID', ''))
        if identifier.version != 7:
            return None
        day = datetime.fromtimestamp(int(identifier.hex[:12], 16) / 1000, timezone.utc)
        base = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
        matches = [path for delta in (-1, 0, 1) for path in
                   (base / (day + timedelta(days=delta)).strftime('%Y/%m/%d')).glob('*' + str(identifier) + '.jsonl')]
        return matches[0] if len(matches) == 1 else None
    except (ValueError, OSError):
        return None


def hook(data, roots):
    cwd = data.get('cwd')
    if not isinstance(cwd, str) or not any(Path(cwd).resolve().is_relative_to(Path(p).resolve()) for p in roots):
        return
    event = data.get('hook_event_name')
    phase = {'UserPromptSubmit': 'prompt', 'PreToolUse': 'start', 'PostToolUse': 'end'}.get(event)
    if not phase and event not in ('Stop', 'Interrupt'):
        return
    values = data.get('tool_input')
    if phase:
        record('hook', phase, operation(data.get('tool_name'), values), inputs=values,
               result=data.get('tool_response'), session=data.get('session_id'),
               call_id=data.get('turn_id') if phase == 'prompt' else data.get('tool_use_id'),
               turn_id=data.get('turn_id'),
               prompt=data.get('prompt') if phase == 'prompt' else None)
    if isinstance(data.get('transcript_path'), str):
        collect_tokens(data['transcript_path'])


def token_report(connection, limit):
    sums = ','.join("SUM(json_extract(data,'$.reported_usage." + k + "'))" for k in TOKEN_KEYS)
    def counts(row):
        result = dict(zip(TOKEN_KEYS, row))
        result['uncached_input_tokens'] = result['input_tokens'] - result['cached_input_tokens']
        return result
    total = connection.execute("SELECT COUNT(*)," + sums + " FROM events WHERE source='codex' AND phase='tokens'").fetchone()
    if not total[0]:
        return {'state': 'unavailable', 'measurement': 'provider_reported', 'measured_steps': 0, 'totals': None}
    cursors = connection.execute('SELECT status,COUNT(*) FROM token_cursors GROUP BY status').fetchall()
    result = {'state': 'observed' if cursors and all(s == 'observed' for s, _ in cursors) else 'partial',
              'measurement': 'provider_reported', 'coverage': 'observed_sources_only', 'totals': counts(total[1:]),
              'measured_steps': total[0], 'sources': {s: n for s, n in cursors},
              'ranked_by': 'uncached_input_plus_output_tokens', 'tasks': [], 'steps': []}
    ranked = "SUM(json_extract(data,'$.reported_usage.input_tokens')-json_extract(data,'$.reported_usage.cached_input_tokens')+json_extract(data,'$.reported_usage.output_tokens'))"
    tasks = connection.execute("SELECT turn,MIN(at),COUNT(*)," + sums + " FROM events WHERE source='codex' AND phase='tokens' AND turn IS NOT NULL GROUP BY turn ORDER BY " + ranked + " DESC LIMIT ?", (limit,)).fetchall()
    for turn, started, steps, *values in tasks:
        label = connection.execute("SELECT data FROM events WHERE turn=? AND phase='prompt' ORDER BY seq LIMIT 1", (turn,)).fetchone()
        closed = connection.execute("SELECT 1 FROM events WHERE source='codex' AND turn=? AND phase='turn_end' LIMIT 1", (turn,)).fetchone()
        result['tasks'].append({'task': turn[:12], 'instruction': json.loads(label[0]).get('instruction') if label else None,
            'first_usage_at': datetime.fromtimestamp(started, timezone.utc).isoformat(), 'turn_closed': bool(closed), 'steps': steps, **counts(values)})
    score = "json_extract(data,'$.reported_usage.input_tokens')-json_extract(data,'$.reported_usage.cached_input_tokens')+json_extract(data,'$.reported_usage.output_tokens')"
    steps = connection.execute("SELECT seq,turn,operation,data FROM events WHERE source='codex' AND phase='tokens' ORDER BY " + score + " DESC LIMIT ?", (limit,)).fetchall()
    for seq, turn, action, text in steps:
        data = json.loads(text)
        result['steps'].append({'step': seq, 'task': turn[:12] if turn else None, 'operation': action,
            'tool_calls': data['tool_calls'], 'tools_truncated': data['tools_truncated'],
            'attribution': data['attribution'], **counts([data['reported_usage'][k] for k in TOKEN_KEYS])})
    result['unassigned_steps'] = connection.execute("SELECT COUNT(*) FROM events WHERE source='codex' AND phase='tokens' AND turn IS NULL").fetchone()[0]
    result['aggregate_intervals'] = connection.execute("SELECT COUNT(*) FROM events WHERE source='codex' AND phase='tokens' AND json_extract(data,'$.attribution')='aggregate_interval'").fetchone()[0]
    if result['unassigned_steps'] or result['aggregate_intervals']:
        result['state'] = 'partial'
    result['measured_tasks'] = connection.execute("SELECT COUNT(DISTINCT turn) FROM events WHERE source='codex' AND phase='tokens' AND turn IS NOT NULL").fetchone()[0]
    result['task_details_truncated'] = result['measured_tasks'] > len(result['tasks'])
    result['details_truncated'] = total[0] > len(result['steps'])
    return result


def report(limit=3):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('usage --limit doit être compris entre 1 et 20')
    source = current_token_source() if os.environ.get('ATELIER_ROUTINES_USAGE') != 'off' else None
    synchronized = collect_tokens(source) if source else {'status': 'disabled_or_source_unavailable'}
    if not (root() / 'events.sqlite3').exists():
        return {'events': 0, 'assessment': 'insufficient_data', 'tokens': {'state': 'unavailable', 'synchronization': synchronized}}
    with database(create=False) as connection:
        connection.execute('BEGIN')
        totals = connection.execute('SELECT source,phase,COUNT(*) FROM events GROUP BY source,phase').fetchall()
        rows = connection.execute("SELECT turn,operation,signature,status FROM events WHERE source='hook' AND phase='end' ORDER BY seq DESC LIMIT ?", (ANALYSIS_LIMIT,)).fetchall()[::-1]
        turns, seen, repeats = defaultdict(list), set(), 0
        branches = defaultdict(set)
        for turn, action, signature, status in rows:
            if not turn:
                continue
            identity = (turn, action, signature)
            repeats += signature is not None and identity in seen
            if signature is not None:
                seen.add(identity)
            if turns[turn]:
                previous, previous_status = turns[turn][-1]
                branches[(previous, previous_status)].add(action)
            turns[turn].append((action, status))
        sequences = Counter(tuple(a for a, _ in actions) for actions in turns.values() if len(actions) > 1)
        statuses = connection.execute("SELECT status,COUNT(*) FROM events WHERE phase='end' GROUP BY status").fetchall()
        top = connection.execute("SELECT source,operation,COUNT(*) FROM events WHERE phase='end' GROUP BY source,operation ORDER BY COUNT(*) DESC LIMIT 10").fetchall()
        pending = connection.execute("SELECT COUNT(*) FROM events s WHERE s.phase='start' AND s.call_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM events e WHERE e.source=s.source AND e.call_id=s.call_id AND e.phase='end')").fetchone()[0]
        result = {'events': sum(n for _, _, n in totals), 'by_source_phase': {s + ':' + p: n for s, p, n in totals},
                  'observed_turns': len(turns), 'repeat_inputs_in_turn': repeats,
                  'reused_sequences': sum(n - 1 for n in sequences.values()),
                  'branching_contexts': sum(len(v) > 1 for v in branches.values()),
                  'recent_hook_results': len(rows), 'analysis_limit': ANALYSIS_LIMIT,
                  'analysis_truncated': sum(n for s, p, n in totals if s == 'hook' and p == 'end') > len(rows),
                  'open_or_missing_results': pending,
                  'status_counts': {s or 'unknown': n for s, n in statuses},
                  'top_operations': [{'source': s, 'id': a, 'count': n} for s, a, n in top],
                  'assessment': 'descriptive_only_compare_rules_first', 'tokens': {**token_report(connection, limit), 'synchronization': synchronized},
                  'task_completion': 'not_observed', 'database_bytes': (root() / 'events.sqlite3').stat().st_size,
                  'database_cap_bytes': CAP_BYTES}
        return result


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--hook':
        raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        try:
            if len(raw) > MAX_HOOK_BYTES:
                record('hook', 'dropped', 'oversized_payload')
            else:
                roots = json.loads(sys.argv[2])
                if not isinstance(roots, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in roots):
                    raise ValueError('Invalid roots')
                data = json.loads(raw)
                if isinstance(data, dict):
                    hook(data, roots)
        except (ValueError, TypeError, OSError):
            record('hook', 'dropped', 'invalid_payload')
