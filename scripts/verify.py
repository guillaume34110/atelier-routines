"""Portable regression suite and three measured before/after stdio scenarios."""
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

from test_mcp import HERE, RTK, connection, make_fixture, rpc


async def benchmark(workspace):
    with tempfile.TemporaryDirectory(dir=workspace) as tmp:
        project = make_fixture(tmp)
        (project / 'src/references.py').write_text(''.join(f"calculate({i})  # {'x' * 80}\n" for i in range(200)))
        cases = [
            ('git.status', 'git status', {}, [
                ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', 'status', '--porcelain=v1', '--branch'],
                ['git', 'diff', '--no-ext-diff', '--no-textconv', '--numstat'],
                ['git', 'diff', '--no-ext-diff', '--no-textconv', '--cached', '--numstat']]),
            ('project.search', 'chercher code', {'pattern': 'calculate', 'path': 'src'}, [
                ['rg', '--no-config', '-n', '--fixed-strings', '--', 'calculate', 'src']]),
            ('files.logs', 'journal erreurs', {'file': 'events.log'}, [
                [sys.executable, '-c',
                 "import re,json;from collections import Counter;from pathlib import Path;lines=Path('events.log').read_text().splitlines();p=re.compile(r'\\b(error|exception|fatal|traceback|timeout|warning|warn)\\b',re.I);print(json.dumps({'lines':len(lines),'matching_lines':sum(bool(p.search(s)) for s in lines),'categories':dict(Counter(m.casefold() for s in lines for m in p.findall(s)))}))"]]),
        ]

        def previous(argv, cwd=project, failed=False):
            started = time.perf_counter()
            result = subprocess.run([RTK, 'proxy', *argv], cwd=cwd, capture_output=True, timeout=30,
                                    env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(tmp)})
            if bool(result.returncode) != failed:
                raise AssertionError(result.stderr.decode()[:500])
            return result.stdout + result.stderr, (time.perf_counter() - started) * 1000

        startup = time.perf_counter()
        async with connection(project, ATELIER_ROUTINES_ROOTS=json.dumps([str(Path(tmp))])) as session:
            startup_ms = (time.perf_counter() - startup) * 1000
            definitions = await session.list_tools()
            rows = []
            for identifier, query, values, commands in cases:
                selection, selection_ms = previous([sys.executable, str(HERE / 'routines.py'), 'match', query])
                old = [previous(argv) for argv in commands]
                started = time.perf_counter()
                found, find_response = await rpc(session, 'routines_find', {'query': identifier, 'limit': 1})
                find_ms = (time.perf_counter() - started) * 1000
                assert found['matches'][0]['id'] == identifier
                started = time.perf_counter()
                body, response = await rpc(session, 'routines_run', {'id': identifier, 'params': {'project': str(project), **values}})
                run_ms = (time.perf_counter() - started) * 1000
                assert body['status'] == 'ok', body
                proof = json.loads(Path(body['evidence'][0]).read_text())
                summary = proof['result']['summary']
                if identifier == 'git.status':
                    def numstat(data):
                        fields = [line.split('\t') for line in data.decode().splitlines()]
                        return {'files': len(fields), 'added': sum(int(f[0]) for f in fields), 'deleted': sum(int(f[1]) for f in fields)}
                    assert summary['unstaged'] == numstat(old[1][0])
                    assert summary['staged'] == numstat(old[2][0])
                    assert summary['status_entries'] == len(old[0][0].splitlines()) - 1
                elif identifier == 'project.search':
                    assert summary['matches'] == len(old[0][0].splitlines()) == 201
                    assert summary['matching_lines'] == 201
                else:
                    assert {k: summary[k] for k in ('lines', 'matching_lines', 'categories')} == json.loads(old[0][0])
                old_bytes = sum(len(output) for output, _ in old)
                run_bytes = len(response.content[0].text.encode())
                rows.append({'scenario': identifier, 'exact': True,
                             'previous_known': {'model_tool_calls': len(commands), 'returned_bytes': old_bytes,
                                                'duration_ms': round(sum(ms for _, ms in old), 2)},
                             'previous_with_lookup': {'model_tool_calls': len(commands) + 1, 'returned_bytes': old_bytes + len(selection),
                                                      'duration_ms': round(selection_ms + sum(ms for _, ms in old), 2)},
                             'mcp_known': {'model_tool_calls': 1, 'returned_bytes': run_bytes, 'duration_ms': round(run_ms, 2)},
                             'mcp_with_lookup': {'model_tool_calls': 2, 'returned_bytes': run_bytes + len(find_response.content[0].text.encode()),
                                                 'duration_ms': round(run_ms + find_ms, 2)},
                             'internal_commands': body['internal_commands'], 'dispatcher_processes': 1})
                if identifier == 'files.logs':
                    repeat_started = time.perf_counter()
                    cached, cached_response = await rpc(session, 'routines_run', {'id': identifier, 'params': {'project': str(project), **values}})
                    assert cached['status'] == 'ok' and cached['reused'] and cached['freshness_verified']
                    assert cached['execution_id'] == body['execution_id']
                    rows[-1]['verified_reuse'] = {'model_tool_calls': 1, 'internal_commands': cached['internal_commands'],
                        'returned_bytes': len(cached_response.content[0].text.encode()),
                        'duration_ms': round((time.perf_counter() - repeat_started) * 1000, 2)}
            nonrepo = Path(tmp) / 'not-a-repository'
            nonrepo.mkdir()
            failure_cases = [
                ('git.status', nonrepo, {}, ['git', 'status', '--porcelain']),
                ('project.search', project, {'pattern': 'calculate', 'path': 'missing'}, ['rg', '-n', '-F', '--', 'calculate', 'missing']),
                ('files.logs', project, {'file': 'missing.log'}, [sys.executable, '-c', "from pathlib import Path;Path('missing.log').read_text()"]),
            ]
            failures = []
            for identifier, target, values, argv in failure_cases:
                output, duration = previous(argv, cwd=target, failed=True)
                started = time.perf_counter()
                body, response = await rpc(session, 'routines_run', {'id': identifier, 'params': {'project': str(target), **values}})
                assert response.isError and body['status'] == 'error', body
                failures.append({'scenario': identifier, 'exact_failure': True,
                    'previous': {'model_tool_calls': 1, 'returned_bytes': len(output), 'duration_ms': round(duration, 2)},
                    'mcp': {'model_tool_calls': 1, 'returned_bytes': len(response.content[0].text.encode()),
                            'duration_ms': round((time.perf_counter() - started) * 1000, 2)}})
            return {'method': 'One measured run per scenario on identical synthetic data. Old traces use the existing CLI selection then the listed native commands, one tool call each. Warm MCP connection; startup reported separately. No model inference time or actual token counts measured. A previous combined script can already use one execution call.',
                    'mcp_startup_ms': round(startup_ms, 2),
                    'tool_definitions_bytes': len(json.dumps([t.model_dump(mode='json') for t in definitions.tools]).encode()),
                    'scenarios': rows, 'failures': failures}


def main():
    if 'TASK_WORKSPACE' not in os.environ:
        if not os.environ.get('USEVPS_JOB_ID'):
            raise SystemExit('Utiliser workspace-budget.py run ; sur VPS, le worker garde le budget complet.')
        workspace = Path.cwd() / 'output/task-tmp'
        workspace.mkdir(parents=True, exist_ok=False)
        os.environ['TASK_WORKSPACE'] = str(workspace)
    workspace = Path(os.environ['TASK_WORKSPACE'])
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    with warnings.catch_warnings():
        warnings.simplefilter('error', SyntaxWarning)
        for path in HERE.glob('*.py'):
            compile(path.read_text(), str(path), 'exec')
    with (workspace / 'tests.log').open('w') as log:
        suite = unittest.defaultTestLoader.discover(str(HERE), pattern='test_*.py')
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    report = {'passed': False, 'tests_passed': result.wasSuccessful(), 'complete': False, 'tests': result.testsRun,
              'failures': len(result.failures), 'errors': len(result.errors),
              'mcp_sdk': importlib.metadata.version('mcp')}
    (workspace / 'validation.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    if not result.wasSuccessful():
        print((workspace / 'tests.log').read_text()[-6000:])
        return 1
    measured = asyncio.run(benchmark(workspace))
    (workspace / 'benchmark.json').write_text(json.dumps(measured, ensure_ascii=False, indent=2))
    report.update(passed=True, complete=True, benchmark_exact=True)
    (workspace / 'validation.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    print(json.dumps(measured, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
