"""Local-only check: launch exactly the personal stdio entry, without exposing config."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    workspace = Path(os.environ['TASK_WORKSPACE'])
    project = Path(__file__).resolve().parents[1]
    entry = tomllib.loads((Path.home() / '.codex/config.toml').read_text())['mcp_servers']['atelier-routines']
    parameters = StdioServerParameters(command=entry['command'], args=entry['args'], env=entry['env'])
    with (workspace / 'installed-server.log').open('w') as log:
        async with stdio_client(parameters, errlog=log) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                assert names == ['routines_find', 'routines_run']
                found = await session.call_tool('routines_find', {'query': 'git.status', 'limit': 1})
                assert not found.isError and json.loads(found.content[0].text)['matches'][0]['id'] == 'git.status'
                response = await session.call_tool('routines_run', {'id': 'git.status', 'params': {'project': str(project)}})
                body = json.loads(response.content[0].text)
                assert not response.isError and body['status'] == 'ok', body
                for arguments in ({'id': 'unknown', 'params': {}},
                                  {'id': 'files.json', 'params': {'project': str(project), 'file': '.env'}}):
                    denied = await session.call_tool('routines_run', arguments)
                    assert denied.isError
    cli = str(Path.home() / '.local/bin/atelier-routines')
    cli_env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
               'ATELIER_ROUTINES_ROOTS': entry['env']['ATELIER_ROUTINES_ROOTS']}
    fallback = subprocess.run([entry['command'], 'proxy', cli, 'run', 'git.status',
                               '--params', json.dumps({'project': str(project)})],
                              env=cli_env, text=True, capture_output=True, timeout=45)
    assert fallback.returncode == 0, fallback.stderr
    other = json.loads(fallback.stdout)
    assert other['status'] == 'ok'
    proof = json.loads(Path(body['evidence'][0]).read_text())
    fallback_proof = json.loads(Path(other['evidence'][0]).read_text())
    assert proof['result']['summary'] == fallback_proof['result']['summary']
    legacy = subprocess.run([entry['command'], 'proxy', cli, 'list'], env=cli_env,
                            text=True, capture_output=True, timeout=15)
    assert legacy.returncode == 0 and '109 routines' in legacy.stdout
    report = {'passed': True, 'server': initialized.serverInfo.name,
              'protocol': initialized.protocolVersion, 'tools': names,
              'search': 'passed', 'execution': body['status'], 'invalid_id_and_path': 'refused',
              'cli_run_matches_mcp': True, 'installed_cli_alias': 'passed', 'legacy_cli_list': 'passed',
              'evidence': body['evidence'] + other['evidence']}
    (workspace / 'installed.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    asyncio.run(main())
