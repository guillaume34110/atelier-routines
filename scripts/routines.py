#!/usr/bin/env python3
"""Small local dispatcher; resource enforcement stays in the canonical guards."""
import argparse
import ast
from collections import Counter
import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

CATALOGUE = Path(__file__).resolve().parents[1] / 'references/catalogue.json'
BIN = Path.home() / '.local/bin'


def words(text):
    text = unicodedata.normalize('NFKD', text.casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    stop = {'a', 'au', 'aux', 'avec', 'ce', 'ces', 'de', 'des', 'du', 'en', 'et', 'faire', 'fais', 'il', 'je', 'la', 'le', 'les', 'mes', 'mon', 'pour', 'que', 'sur', 'the', 'to', 'tu', 'un', 'une'}
    return {w for w in re.findall(r'[a-z0-9]+', text) if w not in stop}


def match(entries, query):
    terms = words(query)
    ranked = []
    for entry in entries:
        if entry['kind'] == 'proposal':
            continue
        phrases = [words(t) for t in [entry['id'], *entry.get('triggers', [])]]
        phrase_score = max((8 * len(p) for p in phrases if p and p <= terms), default=0)
        score = phrase_score + 2 * len(terms & words(entry['id'])) + len(terms & words(entry['description']))
        if query.strip() == entry['id']:
            score += 1000
        if score >= 4:
            ranked.append((score, entry))
    return [entry for _, entry in sorted(ranked, key=lambda pair: (-pair[0], pair[1]['id']))[:3]]


def selection(entries, query):
    selected = match(entries, query)
    if not selected:
        return 'Aucune correspondance fiable ; affiner les mots-clés ou utiliser list DOMAINE. Aucune action exécutée.'
    result = json.dumps({k: v for k, v in selected[0].items() if k != 'triggers'}, ensure_ascii=False, indent=2)
    if len(selected) > 1:
        result += '\nAutres pistes : ' + ', '.join(e['id'] for e in selected[1:])
    return result + '\nProcédure sélectionnée seulement ; aucune action exécutée.'


def emit(text):
    text = str(text)
    print(text if len(text) <= 1950 else text[:1850] + '\n[Sortie abrégée ; affiner la requête.]')


def command(action, project, gib, tail):
    prefix = ['rtk', 'proxy', sys.executable]
    if action == 'storage':
        if gib is None or not math.isfinite(gib) or gib <= 0:
            raise ValueError('storage exige --gib avec un pic positif.')
        return prefix + [str(BIN / 'storage-guard.py'), '--volume', str(project), '--require-gib', str(gib)]
    guard = prefix + [str(BIN / 'workspace-budget.py')]
    if action == 'snapshot':
        return guard + ['snapshot', str(project)]
    if action == 'budget':
        if gib is None or not math.isfinite(gib) or not 0 < gib <= 5:
            raise ValueError('budget exige --gib entre 0 et 5 Gio.')
        if not tail or tail[0] != 'rtk':
            raise ValueError('Après --, fournir une commande préfixée par rtk.')
        return guard + ['--temp-gib', str(gib), 'run', str(project), '--'] + tail
    raise ValueError('Action de garde inconnue.')


def context(project):
    import executor
    lines = [f'Projet : {project}', 'Contrôles exécutés : aucun.']
    manifest = project / 'package.json'
    if manifest.is_file():
        if manifest.stat().st_size > 1024 * 1024:
            raise ValueError('Manifeste trop volumineux pour cette inspection courte.')
        data = json.loads(executor.read_file(project, 'package.json', 1024 * 1024))
        scripts = data.get('scripts', {})
        lines.append(f'Scripts package.json ({len(scripts)}) :')
        lines.extend(f'  {key}: {str(value)[:140]}' for key, value in list(scripts.items())[:8])
        if len(scripts) > 8:
            lines.append('  … voir package.json pour les autres scripts.')
    markers = ['pyproject.toml', 'requirements.txt', 'Package.swift', 'Cargo.toml', 'package-lock.json', 'pnpm-lock.yaml']
    lines.append('Manifestes/verrous : ' + ', '.join(name for name in markers if (project / name).is_file()))
    lines.append('Workflows à lire : ' + ', '.join(str(p.relative_to(project)) for p in sorted((project / '.github/workflows').glob('*')) if p.suffix in ('.yml', '.yaml'))[:350])
    if (project / '.git').exists():
        text, _ = executor.git(project, 'status', '--short', '--branch')
        lines.append('Git (code 0) : ' + text[:300].strip())
    return '\n'.join(lines)


def tree(project):
    exclusions = ['node_modules', '.git', 'dist', 'output', 'outputs', '.venv', '__pycache__', '.pytest_cache']
    cmd = ['rtk', 'proxy', 'rg', '--no-config', '--no-ignore', '--files', '--hidden', '-g', '!**/CLAUDE.md', '-g', '!CLAUDE.md', '-g', '!**/.env*']
    for name in exclusions:
        cmd += ['-g', f'!**/{name}/**']
    result = subprocess.run(cmd, cwd=project, text=True, capture_output=True, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError(f'Inventaire rg indisponible (code {result.returncode}) : {result.stderr[:300]}')
    files = result.stdout.splitlines()
    folders = Counter(name.split('/')[0] if '/' in name else '(racine)' for name in files)
    extensions = Counter(Path(name).suffix.lower() or '(sans extension)' for name in files)
    return '\n'.join([f'Projet : {project}', f'Fichiers hors exclusions : {len(files)}',
                      'Dossiers : ' + ', '.join(f'{name}={count}' for name, count in folders.most_common(12)),
                      'Extensions : ' + ', '.join(f'{name}={count}' for name, count in extensions.most_common(12)),
                      'Métadonnées de chemins seulement ; aucun contenu de source ou média lu.'])


def outline(path):
    if path.name == 'CLAUDE.md' or path.suffix != '.py':
        raise ValueError('outline accepte uniquement un fichier Python ; CLAUDE.md est interdit.')
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Fichier trop volumineux pour cette inspection courte.')
    import executor
    parsed = ast.parse(executor.read_file(path.parent.resolve(), path.name, 2 * 1024 * 1024))
    nodes = sorted((node for node in ast.walk(parsed) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))), key=lambda node: node.lineno)
    lines = [f'Fichier : {path}', f'Déclarations Python : {len(nodes)}']
    lines += [f'{node.lineno}: {type(node).__name__} {node.name}' for node in nodes[:30]]
    if len(nodes) > 30:
        lines.append('Liste abrégée ; cibler ensuite le symbole utile avec rg.')
    return '\n'.join(lines)


def self_test():
    project = Path("/tmp/project with spaces and $literal")
    cmd = command('budget', project, .1, ['rtk', 'proxy', 'python3', 'a b.py'])
    assert cmd[-7:] == ['run', str(project), '--', 'rtk', 'proxy', 'python3', 'a b.py']
    assert command('storage', project, 2, [])[-3:] == [str(project), '--require-gib', '2']
    assert command('snapshot', project, None, [])[-2:] == ['snapshot', str(project)]
    for action, gib, tail in [('budget', 6, ['rtk']), ('budget', .1, ['python3']), ('storage', float('nan'), [])]:
        try:
            command(action, project, gib, tail)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid guarded command accepted')
    entries = json.loads(CATALOGUE.read_text())
    assert len({entry['id'] for entry in entries}) == len(entries)
    assert all(len(json.dumps(entry, ensure_ascii=False, indent=2)) < 1950 for entry in entries)
    emit('Self-check : arguments littéraux, gardes, plafonds et catalogue OK ; aucun job lancé.')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = []
    if '--' in argv:
        split = argv.index('--')
        argv, tail = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['match', 'list', 'show', 'context', 'tree', 'outline', 'preflight', 'storage', 'budget', 'snapshot', 'self-test', 'find', 'run'])
    parser.add_argument('query', nargs='?', default='')
    parser.add_argument('--project', default='.')
    parser.add_argument('--gib', type=float)
    parser.add_argument('--file')
    parser.add_argument('--params', help='Objet JSON pour run ; aucun shell évalué.')
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--request-id', help='Clé stable de demande pour retrouver une exécution interrompue.')
    args = parser.parse_args(argv)
    try:
        if args.params is not None and args.action != 'run':
            raise ValueError('--params est réservé à run.')
        if args.request_id and args.action != 'run':
            raise ValueError('--request-id est réservé à run.')
        if args.action in ('find', 'run'):
            if tail or args.file or args.gib is not None or args.project != '.':
                raise ValueError('find/run utilisent uniquement la requête, --limit ou --params.')
            import executor
            try:
                if args.action == 'find':
                    result = executor.find(args.query, args.limit)
                else:
                    import asyncio
                    import executions
                    result = asyncio.run(executions.dispatch(args.query, json.loads(args.params or '{}'), allow_background=False, request_id=args.request_id))
            except executor.Refusal as error:
                result = {'status': error.status, 'message': str(error)}
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('status', 'ok') == 'ok' else 2
        if args.query and args.action not in ('match', 'list', 'show'):
            raise ValueError('Utiliser --project pour choisir un projet.')
        if tail and args.action != 'budget':
            raise ValueError('Seule l’action budget accepte une commande après --.')
        if args.file and args.action != 'outline':
            raise ValueError('--file est réservé à outline.')
        if args.action == 'self-test':
            self_test()
            return 0
        if args.action == 'outline':
            if not args.file:
                raise ValueError('outline exige --file CHEMIN.py.')
            emit(outline(Path(args.file).expanduser()))
            return 0
        if args.action in ('match', 'list', 'show'):
            entries = json.loads(CATALOGUE.read_text())
            if args.action == 'match':
                if not words(args.query):
                    raise ValueError('match exige une demande ou des mots-clés.')
                emit(selection(entries, args.query))
            elif args.action == 'show':
                entry = next((e for e in entries if e['id'] == args.query), None)
                if entry is None:
                    raise ValueError('Procédure inconnue ; utiliser list.')
                emit(json.dumps(entry, ensure_ascii=False, indent=2))
            else:
                if args.query:
                    emit('\n'.join(f"{e['id']} — {e['description']}" for e in entries if args.query.casefold() in (e['id'] + ' ' + e['description']).casefold()))
                else:
                    active = [e for e in entries if e['kind'] != 'proposal']
                    counts = Counter(e['id'].split('.')[0] for e in active)
                    emit(f'{len(active)} routines disponibles ; {len(entries) - len(active)} propositions exclues du sélecteur.\n'
                         + '\n'.join(f'{domain} : {count}' for domain, count in sorted(counts.items()))
                         + '\nmatch "DEMANDE" : procédure utile ; list DOMAINE : index ciblé ; show ID : détail.')
            return 0
        project = Path(args.project).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError('--project doit désigner un dossier existant.')
        if args.action == 'context':
            emit(context(project))
        elif args.action == 'tree':
            emit(tree(project))
        elif args.action == 'preflight':
            lines = [f'Espace libre destination : {shutil.disk_usage(project).free / 2**30:.1f} Gio']
            for cmd in [['rtk', 'proxy', 'memory_pressure', '-Q'], ['rtk', 'proxy', 'sysctl', 'vm.swapusage']]:
                r = subprocess.run(cmd, text=True, capture_output=True, timeout=15)
                lines.append((r.stdout or r.stderr)[:550].strip())
            emit('\n'.join(lines))
        else:
            return subprocess.run(command(args.action, project, args.gib, tail)).returncode
        return 0
    except (ValueError, OSError, SyntaxError, subprocess.TimeoutExpired) as error:
        emit(f'Erreur : {error}')
        return 2


if __name__ == '__main__':
    sys.exit(main())
