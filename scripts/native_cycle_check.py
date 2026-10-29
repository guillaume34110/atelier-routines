"""Mac-only real installed MCP/VPS cycle; creates one compute and resumes its collection."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import time
import tomllib

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    workspace = Path(os.environ['TASK_WORKSPACE'])
    project = Path(__file__).resolve().parents[1]
    entry = tomllib.loads((Path.home() / '.codex/config.toml').read_text())['mcp_servers']['atelier-routines']
    env = dict(entry['env'])
    if os.environ.get('SSH_AUTH_SOCK'):
        env['SSH_AUTH_SOCK'] = os.environ['SSH_AUTH_SOCK']
    params = StdioServerParameters(command=entry['command'], args=entry['args'], env=env)
    measurements = []
    with (workspace / 'native-server.log').open('w') as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=3700)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == 'atelier-routines'
                definitions = await session.list_tools()
                assert [t.name for t in definitions.tools] == ['routines_find', 'routines_run']

                async def call(identifier, values, request_id=None):
                    started = time.perf_counter()
                    arguments = {'id': identifier, 'params': {'project': str(project), **values}}
                    if request_id:
                        arguments['request_id'] = request_id
                    response = await session.call_tool('routines_run', arguments)
                    body = json.loads(response.content[0].text)
                    measurements.append({'id': identifier, 'status': body['status'],
                                         'returned_bytes': len(response.content[0].text.encode()),
                                         'duration_ms': round((time.perf_counter() - started) * 1000, 2)})
                    return body

                recipe = json.loads((project / 'examples/project-review.json').read_text())
                registered = await call('routines.register', recipe)
                assert registered['status'] == 'ok', registered
                reviewed = await call('custom.atelier-project-review', {'term': 'Refusal'})
                assert reviewed['status'] == 'ok', reviewed
                assert reviewed['result']['summary']['steps_completed'] == 2
                started = await call('checks.regression', {'wait': False})
                assert started['status'] in ('starting', 'running'), started
                execution_id = started['execution_id']
                directory = project / 'output/routines-runs' / execution_id
                directory.mkdir(mode=0o700, parents=True, exist_ok=False)
                sentinel = directory / 'validation.json'
                owned = b'{"test_owned_collection_conflict":true}'
                with sentinel.open('xb') as target:
                    target.write(owned)
                sentinel.chmod(0o600)
                interrupted = await call('executions.wait', {'execution_id': execution_id})
                assert interrupted['status'] == 'decision_required', interrupted
                assert interrupted['remote_job'] and not interrupted['released'], interrupted
                assert sentinel.read_bytes() == owned
                sentinel.unlink()
                resumed = await call('executions.resume', {'execution_id': execution_id})
                assert resumed['status'] == 'ok', resumed
                assert resumed['validation_status'] == 'passed' and resumed['released'], resumed
                assert resumed['remote_job'] == interrupted['remote_job'], resumed
                assert resumed['submission_attempts'] == 1, resumed
                verified = await call('executions.inspect', {'execution_id': execution_id})
                assert verified['status'] == 'ok' and verified['released'], verified
                report = {'passed': True, 'real_vps_cycle': True, 'supported_resume': True,
                          'submission_attempts': resumed['submission_attempts'], 'execution_id': execution_id,
                          'remote_job': resumed['remote_job'], 'evidence': resumed['evidence'],
                          'measurements': measurements, 'long_cycle_before_trace_available': False}
                (workspace / 'native-validation.json').write_text(json.dumps(report, indent=2))
                print(json.dumps(report))


if __name__ == '__main__':
    asyncio.run(main())
