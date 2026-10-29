#!/usr/bin/env python3
"""Two small stdio tools, backed by the existing catalogue and shared executor."""
import asyncio
import json
import os
import signal

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

import executor
import executions

server = Server('atelier-routines')


@server.list_tools()
async def list_tools():
    return [
        Tool(name='routines_find', description='Trouver au plus 3 routines : ID, disponibilité, paramètres. Aucune exécution.',
             inputSchema={'type': 'object', 'properties': {
                 'query': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'default': 3}},
                 'required': ['query'], 'additionalProperties': False},
             annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False)),
        Tool(name='routines_run', description='ID connu : exécuter avec son schéma. Statut explicite, résultat compact et preuves. Procédures guidées signalées.',
             inputSchema={'type': 'object', 'properties': {
                 'id': {'type': 'string', 'maxLength': 100}, 'params': {'type': 'object'},
                 'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 64}},
                 'required': ['id', 'params'], 'additionalProperties': False},
             annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False)),
    ]


@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    try:
        allowed = {'query', 'limit'} if name == 'routines_find' else {'id', 'params', 'request_id'}
        required = {'query'} if name == 'routines_find' else {'id', 'params'}
        if type(arguments) is not dict or set(arguments) - allowed or not required <= arguments.keys():
            raise executor.Refusal('Paramètres d’outil manquants ou inconnus.')
        if name == 'routines_find':
            result = executor.find(arguments['query'], arguments.get('limit', 3))
        elif name == 'routines_run':
            result = await executions.dispatch(arguments['id'], arguments['params'], request_id=arguments.get('request_id'))
        else:
            result = {'status': 'error', 'message': 'Outil inconnu.'}
    except executor.Refusal as error:
        result = {'status': error.status, 'message': str(error)}
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
                          isError=result.get('status') in ('error', 'timeout', 'unavailable'))


async def main():
    def terminate(signum, frame):
        executor.INTERRUPTED = True
        for process in list(executor.OWNED.values()):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        import vps_cycle
        await vps_cycle.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
