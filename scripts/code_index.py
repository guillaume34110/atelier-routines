"""Private, portable references to source files under the configured roots."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import stat
import time

import executor as core


EXTENSIONS = {'.bash', '.c', '.cc', '.clj', '.cljs', '.cpp', '.cs', '.css', '.dart',
              '.ex', '.exs', '.go', '.h', '.hh', '.hpp', '.html', '.java', '.jl',
              '.js', '.jsx', '.kt', '.kts', '.lua', '.m', '.mjs', '.mm', '.php',
              '.pl', '.py', '.r', '.rb', '.rs', '.scala', '.scss', '.sh', '.sql',
              '.svelte', '.swift', '.ts', '.tsx', '.vue', '.zsh'}
DECLARATION = re.compile(r'^\s*(?:(?:export|default|public|private|protected|internal|open|static|final|abstract|async|suspend|pub)\s+)*(?:function|func|fun|def|class|interface|struct|enum|type|trait|protocol|fn)\s+([A-Za-z_$][\w$]*)')
ARROW = re.compile(r'^\s*(?:export\s+)?(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=.*=>')
MAX_CONTENT = 2 * 1024 * 1024
MAX_FILES = 300_000
MAX_SECONDS = 150
CAP_BYTES = 256 * 1024 * 1024
PRUNED = core.EXCLUDED | {'venv', 'vendor', 'site-packages', 'Pods', 'DerivedData', '.gradle', '.build'}


def roots():
    selected = []
    for root in sorted(set(core.roots()), key=lambda value: (len(value.parts), str(value))):
        if not any(root.is_relative_to(parent) for parent in selected):
            selected.append(root)
    return selected


def location():
    import workflows
    store = workflows.root()
    return store.with_name(store.name + '-code')


@contextmanager
def database():
    directory = location()
    with core.directory_fd(directory, create=True) as parent:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise core.Refusal('Index de code non privé (0700).')
        handle = os.open('index.sqlite3', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise core.Refusal('Fichier d’index non privé ou non régulier.')
        finally:
            os.close(handle)
        connection = sqlite3.connect(directory / 'index.sqlite3', timeout=2)
        try:
            connection.execute('PRAGMA max_page_count=' + str(CAP_BYTES // connection.execute('PRAGMA page_size').fetchone()[0]))
            application = connection.execute('PRAGMA application_id').fetchone()[0]
            if application == 0:
                with connection:
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise core.Refusal('Base d’index étrangère ; aucune modification.')
                    connection.execute('PRAGMA application_id=1096041295')
                    connection.execute('''CREATE TABLE files (
                        path TEXT PRIMARY KEY, root TEXT NOT NULL, project TEXT NOT NULL,
                        language TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                        ctime_ns INTEGER NOT NULL, inode INTEGER NOT NULL, sha256 TEXT,
                        symbols TEXT NOT NULL, seen INTEGER NOT NULL)''')
                    connection.execute('CREATE INDEX files_root ON files(root)')
            elif application != 1096041295 or connection.execute('PRAGMA user_version').fetchone()[0] != 0:
                raise core.Refusal('Version de l’index inconnue ; aucune modification.')
            yield connection
        except sqlite3.Error as error:
            raise core.Refusal('Index de code indisponible : ' + str(error)[:120]) from None
        finally:
            connection.close()


def inspect(path, expected):
    if expected.st_size > MAX_CONTENT:
        return None, '[]'
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(handle, 'rb') as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns, expected.st_ctime_ns)):
            raise OSError('Source modifiée pendant l’indexation.')
        content = source.read(MAX_CONTENT + 1)
        after = os.fstat(source.fileno())
    if len(content) != before.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
        raise OSError('Source modifiée pendant l’indexation.')
    try:
        lines = content[:256 * 1024].decode('utf-8-sig').splitlines()
    except UnicodeDecodeError:
        lines = []
    symbols = []
    for line, source in enumerate(lines, 1):
        found = DECLARATION.match(source) or ARROW.match(source)
        if found:
            symbols.append([found[1], line])
            if len(symbols) == 128:
                break
    return hashlib.sha256(content).hexdigest(), json.dumps(symbols, ensure_ascii=False)


def scan(connection):
    started, generation = time.monotonic(), time.time_ns()
    selected = roots()
    counts = {'files': 0, 'updated': 0, 'reused': 0, 'unhashed': 0, 'unreadable': 0, 'removed': 0}
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        authorized = {str(root) for root in selected}
        for old_root, in connection.execute('SELECT DISTINCT root FROM files'):
            if old_root not in authorized:
                counts['removed'] += connection.execute('DELETE FROM files WHERE root=?', (old_root,)).rowcount
        for root in selected:
            previous_files = {row[0]: row[1:] for row in connection.execute(
                'SELECT path,size,mtime_ns,ctime_ns,inode,sha256 IS NULL FROM files WHERE root=?', (str(root),))}
            visited = set()
            projects = {str(root): str(root)}
            def onerror(_error):
                counts['unreadable'] += 1
            for base, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
                if time.monotonic() - started > MAX_SECONDS:
                    raise core.Refusal('Indexation trop longue après ' + str(counts['files']) + ' sources ; ancien index conservé.', 'partial')
                folder = Path(base)
                parent = projects.get(str(folder.parent), str(root))
                project = str(folder) if (folder / '.git').exists() else parent
                projects[str(folder)] = project
                directories[:] = sorted(name for name in directories if name not in PRUNED
                    and not core.forbidden(folder / name) and not (folder / name).is_symlink())
                for name in sorted(files):
                    path = folder / name
                    if path.suffix.lower() not in EXTENSIONS or core.forbidden(path):
                        continue
                    try:
                        info = path.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            continue
                        counts['files'] += 1
                        if counts['files'] > MAX_FILES:
                            raise core.Refusal('Index limité à 300 000 sources ; ancien index conservé.', 'partial')
                        name = str(path)
                        visited.add(name)
                        previous = previous_files.get(name)
                        if previous and previous[:4] == (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino):
                            counts['reused'] += 1
                            counts['unhashed'] += previous[4]
                            continue
                        else:
                            sha256, symbols = inspect(path, info)
                            counts['updated'] += 1
                        counts['unhashed'] += sha256 is None
                        connection.execute('''INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(path) DO UPDATE SET root=excluded.root,project=excluded.project,
                            language=excluded.language,size=excluded.size,mtime_ns=excluded.mtime_ns,
                            ctime_ns=excluded.ctime_ns,inode=excluded.inode,sha256=excluded.sha256,
                            symbols=excluded.symbols,seen=excluded.seen''',
                            (name, str(root), project, path.suffix.lower().lstrip('.'), info.st_size,
                             info.st_mtime_ns, info.st_ctime_ns, info.st_ino, sha256, symbols, generation))
                    except OSError:
                        counts['unreadable'] += 1
            if not counts['unreadable']:
                for missing in previous_files.keys() - visited:
                    counts['removed'] += connection.execute('DELETE FROM files WHERE path=?', (missing,)).rowcount
    counts['indexed_files'] = connection.execute('SELECT COUNT(*) FROM files').fetchone()[0]
    counts['roots'] = [str(root) for root in selected]
    counts['scope'] = 'Sources par extension, sans liens ni chemins protégés ; symboles lexicaux limités à 128 et aux premiers 256 Kio.'
    return counts


def find(connection, pattern, summary):
    needle = pattern.casefold()
    authorized = set(summary['roots'])
    rows = connection.execute('''SELECT path,project,language,size,sha256,symbols FROM files
        WHERE instr(lower(path),?)>0 OR instr(lower(symbols),?)>0 LIMIT 501''', (needle, needle)).fetchall()
    candidates = {row[0]: row for row in rows[:500] if any(Path(row[0]).is_relative_to(root) for root in authorized)}
    limited = len(rows) > 500
    if len(candidates) < 10:
        glob = '*.{'+','.join(sorted(suffix.lstrip('.') for suffix in EXTENSIONS))+'}'
        for root in roots():
            output, truncated = core.rg(root, '-l', '-F', '-i', '--max-filesize', '2M', '--iglob', glob, '--', pattern, '.')
            names = output.splitlines()
            if truncated:
                names = names[:-1]
            limited |= truncated
            for name in names[:200]:
                if core.forbidden(name):
                    continue
                path = str(core.local_path(root, name))
                if path not in candidates:
                    row = connection.execute('SELECT path,project,language,size,sha256,symbols FROM files WHERE path=? AND root=?', (path, str(root))).fetchone()
                    if row:
                        candidates[path] = row
            limited |= len(names) > 200
    def score(row):
        path, _, _, _, _, symbols = row
        names = [item[0].casefold() for item in json.loads(symbols)]
        stem = Path(path).stem.casefold()
        return (0 if needle == stem or needle in names else 1 if needle in stem else 2 if any(needle in name for name in names) else 3, path)
    ordered = sorted(candidates.values(), key=score)
    details = []
    for path, project, language, size, sha256, symbols in ordered[:10]:
        symbol_hits = [{'name': name, 'line': line} for name, line in json.loads(symbols) if needle in name.casefold()][:5]
        try:
            source = Path(path)
            current = bool(sha256 and inspect(source, source.stat(follow_symlinks=False))[0] == sha256)
        except OSError:
            current = False
        duplicates = connection.execute('SELECT COUNT(*) FROM files WHERE sha256=?', (sha256,)).fetchone()[0] if sha256 else 0
        details.append({'path': path, 'project': project, 'language': language, 'bytes': size,
                        'sha256': sha256, 'current': current, 'symbols': symbol_hits,
                        'identical_files': duplicates, 'qa': 'not_assessed'})
    return {'query': pattern, 'matches_seen': len(candidates), 'returned': len(details),
            'indexed_files': summary['indexed_files'], 'roots': summary['roots'],
            'source_copied': False, 'qa_certified': False}, details, limited or len(ordered) > len(details)


def execute(identifier, params):
    with database() as connection:
        summary = scan(connection)
        if identifier == 'code.index':
            return {'summary': summary, 'details': [], 'partial': bool(summary['unreadable']),
                    'internal_commands': core.COMMANDS}
        result, details, limited = find(connection, params['pattern'], summary)
        result['scan'] = {key: summary[key] for key in ('updated', 'reused', 'unhashed', 'unreadable', 'removed')}
        return {'summary': result, 'details': details, 'partial': limited or bool(summary['unreadable']),
                'internal_commands': core.COMMANDS}
