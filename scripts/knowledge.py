"""Private, evidence-linked R&D findings shared across authorized projects."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import code_index
import executor as core


FIELDS = ('topic', 'question', 'conditions', 'finding')


def key(text):
    plain = ''.join(ch for ch in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(ch))
    return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in plain).split())


def prepare(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS research (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, topic TEXT NOT NULL,
        question TEXT NOT NULL, conditions TEXT NOT NULL, finding TEXT NOT NULL,
        reported_result TEXT NOT NULL, evidence_file TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL, supersedes TEXT NOT NULL,
        created_at TEXT NOT NULL, topic_key TEXT NOT NULL, search_key TEXT NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS research_supersedes ON research(supersedes)')


def record(connection, project, params):
    values = {name: core.redact(params[name].strip()) for name in FIELDS}
    if not all(values.values()):
        raise core.Refusal('Thème, question, conditions et conclusion doivent contenir du texte.')
    file = params['evidence_file']
    digest = hashlib.sha256(core.read_bytes(project, file, 2 * 1024 * 1024)).hexdigest() if file else ''
    supersedes = params['supersedes']
    if supersedes and not re.fullmatch(r'[0-9a-f]{32}', supersedes):
        raise core.Refusal('Référence remplacée invalide.')
    note = {**values, 'project': str(project), 'reported_result': params['reported_result'],
            'evidence_file': file, 'evidence_sha256': digest, 'supersedes': supersedes}
    identifier = hashlib.sha256(json.dumps(note, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        prepare(connection)
        exists = connection.execute('SELECT 1 FROM research WHERE id=?', (identifier,)).fetchone()
        if supersedes and not exists:
            previous = connection.execute('SELECT topic FROM research WHERE id=?', (supersedes,)).fetchone()
            if not previous:
                raise core.Refusal('Conclusion à remplacer introuvable.')
            if key(previous[0]) != key(values['topic']):
                raise core.Refusal('Une correction conserve le même thème.')
            if connection.execute('SELECT 1 FROM research WHERE supersedes=?', (supersedes,)).fetchone():
                raise core.Refusal('Conclusion déjà remplacée ; utiliser sa version courante.')
        if not exists:
            connection.execute('''INSERT INTO research VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier, str(project), values['topic'], values['question'], values['conditions'],
                 values['finding'], params['reported_result'], file, digest, supersedes,
                 datetime.now(timezone.utc).isoformat(), key(values['topic']),
                 key(' '.join([*values.values(), params['reported_result'], file]))))
    return {'summary': {'id': identifier, 'created': not bool(exists), 'topic': values['topic'],
                        'reported_result': params['reported_result'], 'evidence_sha256': digest or None,
                        'evidence_current': True if digest else None, 'qa_certified': False},
            'details': [], 'partial': False, 'internal_commands': core.COMMANDS}


def find(connection, params):
    query = key(params['query'])
    if not query:
        raise core.Refusal('Recherche vide après normalisation.')
    selected = code_index.roots()
    details = []
    matches = 0
    changed = 0
    for row in connection.execute('''SELECT id,project,topic,question,conditions,finding,reported_result,
            evidence_file,evidence_sha256,supersedes,created_at FROM research AS current
            WHERE instr(search_key,?)>0 AND NOT EXISTS
                (SELECT 1 FROM research AS newer WHERE newer.supersedes=current.id)
            ORDER BY CASE WHEN topic_key=? THEN 0 WHEN instr(topic_key,?)>0 THEN 1 ELSE 2 END,
                     created_at DESC''', (query, query, query)):
        identifier, project, topic, question, conditions, finding, result, file, digest, supersedes, created = row
        if not any(Path(project).is_relative_to(root) for root in selected):
            continue
        matches += 1
        if len(details) == 10:
            continue
        current = None
        if file:
            try:
                current = hashlib.sha256(core.read_bytes(Path(project), file, 2 * 1024 * 1024)).hexdigest() == digest
            except (core.Refusal, OSError):
                current = False
            changed += not current
        details.append({'id': identifier, 'project': project, 'topic': topic, 'question': question,
                        'conditions': conditions, 'finding': finding, 'reported_result': result,
                        'evidence_file': file or None, 'evidence_sha256': digest or None,
                        'evidence_current': current, 'supersedes': supersedes or None,
                        'created_at': created})
    return {'summary': {'query': params['query'], 'matches_seen': matches, 'returned': len(details),
                        'changed_evidence': changed, 'qa_certified': False,
                        'source_copied': False}, 'details': details, 'partial': matches > len(details),
            'internal_commands': core.COMMANDS}


def execute(identifier, project, params):
    with code_index.database() as connection:
        if identifier == 'knowledge.record':
            return record(connection, project, params)
        with connection:
            prepare(connection)
        return find(connection, params)
