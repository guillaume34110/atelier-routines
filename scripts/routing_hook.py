"""Route covered inspections before shell execution; never executes the command."""
import json
from pathlib import Path
import shlex
import sys

PRIORITY = ('Chaque étape couverte, même ponctuelle : atelier-routines d’abord, pour le parent et les sous-agents. '
            'IDs connus : git.status/diff/staged/log/branches, project.manifest/tree/search, files.logs/json/csv. '
            'Appeler routines_run directement ; sinon une seule routines_find. CLI seulement si MCP indisponible. '
            'Lecture exacte pour éditer ou opération hors couverture : outils directs. Une simple question ne lance rien.')


def route(words, cwd, roots):
    if words and Path(words[0]).name == 'rtk':
        words = words[2:] if words[1:2] == ['proxy'] else words[1:]
    if not words:
        return None
    command, args = Path(words[0]).name, words[1:]
    project = Path(cwd).resolve()
    if command == 'git':
        while args:
            if args[0] == '--no-optional-locks':
                args = args[1:]
            elif args[0] == '-C' and len(args) >= 2:
                project = (project / args[1]).resolve()
                args = args[2:]
            else:
                break
    if not project.is_dir() or not any(project.is_relative_to(root) for root in roots):
        return None
    if command == 'git':
        for parent in (project, *project.parents):
            if not any(parent.is_relative_to(root) for root in roots):
                break
            if (parent / '.git').exists():
                project = parent
                break
    params = {'project': str(project)}
    if command == 'git' and args:
        verb, flags = args[0], args[1:]
        if verb == 'status' and all(f in {'-s', '-b', '-sb', '--short', '--branch', '--porcelain', '--porcelain=v1'} for f in flags):
            return 'git.status', params
        if verb == 'branch' and (not flags or set(flags) <= {'--list', '-v', '-vv'}):
            return 'git.branches', params
        if verb == 'branch' and flags == ['--show-current']:
            return 'git.status', params
        summaries = {'--stat', '--numstat', '--shortstat', '--name-only', '--name-status'}
        if verb == 'diff' and summaries.intersection(flags) and set(flags) <= summaries | {
                '--cached', '--staged', '--no-ext-diff', '--no-textconv', '--no-color'}:
            return ('git.staged' if {'--cached', '--staged'}.intersection(flags) else 'git.diff'), params
        if verb == 'log':
            limit, rest = 8, list(flags)
            while rest:
                flag = rest.pop(0)
                if flag in {'--oneline', '--no-decorate'}:
                    continue
                if flag in {'-n', '--max-count'} and rest:
                    limit = int(rest.pop(0))
                elif flag.startswith('--max-count='):
                    limit = int(flag.split('=', 1)[1])
                elif flag.startswith('-') and flag[1:].isdigit():
                    limit = int(flag[1:])
                else:
                    return None
            if 1 <= limit <= 30:
                return 'git.log', {**params, 'limit': limit}
    if command == 'rg':
        if args and '--files' in args and set(args) <= {'--files', '--hidden', '--no-ignore', '--no-config', '.', '--'}:
            return 'project.tree', params
        flags, rest = set(), list(args)
        while rest and rest[0].startswith('-'):
            flag = rest.pop(0)
            if flag == '--':
                break
            flags.add(flag)
        if (flags.intersection({'-F', '--fixed-strings'}) and flags <= {
                '-F', '--fixed-strings', '-n', '--line-number', '--hidden', '--no-ignore', '--no-config'}
                and 1 <= len(rest) <= 2 and 1 <= len(rest[0]) <= 200):
            path = (project / (rest[1] if len(rest) == 2 else '.')).resolve()
            if path.is_relative_to(project):
                relative = str(path.relative_to(project))
                if len(relative) <= 1024:
                    return 'project.search', {**params, 'pattern': rest[0], 'path': relative}
    return None


def response(data, roots):
    event = data.get('hook_event_name')
    if event in {'UserPromptSubmit', 'SubagentStart'}:
        return {'hookEventName': event, 'additionalContext': PRIORITY}
    if event != 'PreToolUse' or data.get('tool_name') not in {'Bash', 'exec_command'}:
        return None
    values = data.get('tool_input', {})
    command = values.get('command', values.get('cmd', ''))
    cwd = values.get('workdir') or data.get('cwd')
    if not isinstance(command, str) or len(command) > 8192 or not isinstance(cwd, str):
        return None
    # Complex shell programs and exact source reads remain outside this small router.
    if '$(' in command or '`' in command:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    tokens = [';' if token and set(token) <= set(';\n') else token for token in lexer]
    if any(t and set(t) <= set('|&()<>') and t not in {'&&', '||'} for t in tokens):
        return None
    words = []
    for token in [*tokens, ';']:
        if token not in {';', '&&', '||', '\n'}:
            words.append(token)
            continue
        selected = route(words, cwd, roots)
        words = []
        if selected:
            identifier, params = selected
            return {'hookEventName': event, 'permissionDecision': 'deny', 'permissionDecisionReason':
                    f'Inspection couverte : appeler MCP atelier-routines routines_run avec id="{identifier}" '
                    f'et params ({", ".join(params)}) repris de cette commande. Ne pas relancer la commande équivalente. '
                    'Si MCP indisponible seulement : rtk proxy atelier-routines run IDENTIFIANT --params OBJET_JSON. '
                    'Les lectures exactes pour éditer et les opérations hors couverture restent directes.'}
    return None


def main():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        return
    try:
        roots = json.loads(sys.argv[1]) if len(sys.argv) == 2 else []
        if not isinstance(roots, list) or len(roots) > 16 or not all(isinstance(p, str) and Path(p).is_absolute() for p in roots):
            return
        data = json.loads(raw)
        result = response(data, [Path(p).resolve() for p in roots]) if isinstance(data, dict) else None
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError):
        return
    if result:
        print(json.dumps({'hookSpecificOutput': result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
