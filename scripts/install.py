"""Append this stdio server without rewriting existing configuration or secrets."""
import importlib.metadata
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import sys
import tempfile
import tomllib


def install_hooks(directory, python, rtk):
    config = directory / 'hooks.json'
    hook_dir = directory / 'hooks'
    if config.is_symlink() or hook_dir.is_symlink():
        raise ValueError('Configuration de hooks symbolique : source préservée.')
    original = config.read_bytes() if config.exists() else b''
    if len(original) > 1024 * 1024:
        raise ValueError('Configuration de hooks trop volumineuse.')
    data = json.loads(original) if original else {}
    content = Path(__file__).with_name('codex_hook.py').read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    hook_dir.mkdir(parents=True, exist_ok=True)
    target = hook_dir / f'atelier-routines-{digest}.py'
    if target.is_symlink() or (target.exists() and target.read_bytes() != content):
        raise ValueError('Hook installé altéré : aucune écriture.')
    if not target.exists():
        with target.open('xb') as out:
            out.write(content)
        target.chmod(0o600)
    command = shlex.join([str(rtk), 'proxy', str(python), str(target)])
    hooks = data.setdefault('hooks', {})
    for event, matcher in [('SessionStart', '^(startup|resume|clear|compact|fork)$'),
                           ('PostToolUse', '^mcp__atelier[-_]routines__routines_run$')]:
        message = f'Atelier routines: {event}'
        groups = hooks.setdefault(event, [])
        owned = [group for group in groups if len(group.get('hooks', [])) == 1
                 and group['hooks'][0].get('statusMessage') == message
                 and 'atelier-routines-' in group['hooks'][0].get('command', '')]
        for group in owned:
            groups.remove(group)
        groups.append({'matcher': matcher, 'hooks': [{'type': 'command', 'command': command,
                       'timeout': 5, 'additionalContextLimit': 220, 'statusMessage': message}]})
    updated = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    if updated == original:
        return
    fd, name = tempfile.mkstemp(prefix='.routines-hooks-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(updated)
            out.flush()
            os.fsync(out.fileno())
        if (config.read_bytes() if config.exists() else b'') != original:
            raise ValueError('Hooks modifiés pendant l’installation : aucune écriture.')
        os.replace(name, config)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install_skill(source, directory):
    directory.mkdir(parents=True, exist_ok=True)
    target, metadata = directory / 'SKILL.md', directory / '.installation.json'
    if directory.is_symlink() or metadata.is_symlink():
        raise ValueError('Dossier ou métadonnées de skill symboliques : installation préservée.')
    content = source.read_bytes()
    previous = json.loads(metadata.read_text()) if metadata.is_file() and not metadata.is_symlink() else {}
    if target.is_symlink():
        if target.resolve() != source.resolve():
            raise ValueError('Skill lié à une autre source ; installation préservée.')
        target.unlink()
    elif target.exists():
        current = target.read_bytes()
        if current != content and hashlib.sha256(current).hexdigest() != previous.get('skill_sha256'):
            raise ValueError('Skill modifié localement ; aucune instruction remplacée.')
    fd, temporary = tempfile.mkstemp(prefix='.skill-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    metadata.write_text(json.dumps({'source': str(source), 'skill_sha256': hashlib.sha256(content).hexdigest()}))
    metadata.chmod(0o600)


def configure(config, python, root, rtk, replace=False):
    if config.is_symlink():
        raise ValueError('Configuration symbolique : modifier sa source explicitement.')
    original = config.read_bytes() if config.exists() else b''
    before = tomllib.loads(original.decode())
    paths = list(dict.fromkeys([str(Path(rtk).parent), *os.defpath.split(os.pathsep)]))
    entry = {'command': str(rtk),
             'args': ['proxy', str(python), str(Path(__file__).resolve().with_name('mcp_server.py'))],
             'startup_timeout_sec': 20, 'tool_timeout_sec': 3700,
             'env': {'ATELIER_ROUTINES_ROOTS': json.dumps([str(root)]),
                     'PYTHONDONTWRITEBYTECODE': '1', 'PATH': os.pathsep.join(paths)}}
    existing = before.get('mcp_servers', {}).get('atelier-routines')
    if existing is not None:
        if existing == entry:
            return entry
        if not replace or not isinstance(existing.get('args'), list) or existing['args'][-1:] != entry['args'][-1:]:
            raise ValueError('Une entrée atelier-routines différente existe ; aucun réglage remplacé.')
    block = '\n[mcp_servers.atelier-routines]\n'
    for key in ('command', 'args', 'startup_timeout_sec', 'tool_timeout_sec'):
        block += key + ' = ' + json.dumps(entry[key]) + '\n'
    block += '\n[mcp_servers.atelier-routines.env]\n'
    block += ''.join(key + ' = ' + json.dumps(value) + '\n' for key, value in entry['env'].items())
    if existing is None:
        updated = original + block.encode()
    else:
        text = original.decode()
        lines = text.splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.strip() == '[mcp_servers.atelier-routines]')
        end = start + 1
        while end < len(lines):
            line = lines[end].strip()
            if line.startswith('[') and not line.startswith('[mcp_servers.atelier-routines.'):
                break
            end += 1
        updated = (''.join(lines[:start]) + block + ''.join(lines[end:])).encode()
    parsed = tomllib.loads(updated.decode())
    assert parsed['mcp_servers'].pop('atelier-routines') == entry
    expected = tomllib.loads(original.decode())
    if existing is not None:
        expected['mcp_servers'].pop('atelier-routines')
    if 'mcp_servers' not in expected:
        parsed.pop('mcp_servers')
    assert parsed == expected
    config.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.routines-config-', dir=config.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(updated)
            out.flush()
            os.fsync(out.fileno())
        if (config.read_bytes() if config.exists() else b'') != original:
            raise ValueError('Configuration modifiée pendant l’installation ; aucune écriture.')
        os.replace(name, config)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return entry


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true', help='Mettre à jour cette installation reconnue uniquement.')
    args = parser.parse_args()
    version = importlib.metadata.version('mcp')
    if version != '1.30.0':
        raise SystemExit('Exécuter avec le SDK MCP 1.30.0 installé ; voir requirements.txt.')
    rtk = shutil.which('rtk')
    if not rtk:
        raise SystemExit('rtk est requis.')
    configure(Path.home() / '.codex/config.toml', sys.executable,
              Path(__file__).resolve().parents[2], rtk, replace=args.replace)
    install_skill(Path(__file__).resolve().parents[1] / 'SKILL.md', Path.home() / '.codex/skills/atelier-routines')
    install_hooks(Path.home() / '.codex', sys.executable, rtk)
    print('atelier-routines configuré en stdio ; skill et hooks installés, autres réglages conservés. Examiner les nouveaux hooks avec /hooks dans Codex.')
