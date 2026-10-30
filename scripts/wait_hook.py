"""Keep existing waits in tools; never launches a job, polls, or reads a transcript."""
import json
import re
import sys

SESSION_MS = 300000
CELL_MS = 900000
VPS_SECONDS = 3600


def number(value, maximum):
    return type(value) in (int, float) and 0 <= value <= maximum


def response(data):
    event = data.get('hook_event_name')
    name = data.get('tool_name', '').removeprefix('functions.')
    args = data.get('tool_input')
    if not isinstance(args, dict):
        return None
    if event == 'PreToolUse':
        updated = dict(args)
        if (name == 'write_stdin' and type(args.get('session_id')) is int
                and args['session_id'] > 0 and args.get('chars', '') == ''
                and number(args.get('yield_time_ms', 0), SESSION_MS)):
            updated['yield_time_ms'] = SESSION_MS
        elif (name == 'wait' and isinstance(args.get('cell_id'), str) and args['cell_id']
              and not args.get('terminate', False) and number(args.get('yield_time_ms', 0), CELL_MS)):
            updated['yield_time_ms'] = CELL_MS
        elif (re.fullmatch(r'mcp__atelier[-_]vps__vps_wait', name)
              and isinstance(args.get('job_id'), str) and args['job_id']
              and number(args.get('timeout', VPS_SECONDS), VPS_SECONDS) and args.get('timeout', VPS_SECONDS) > 0):
            updated['timeout'] = VPS_SECONDS
        elif (re.fullmatch(r'mcp__atelier[-_]routines__routines_run', name)
              and args.get('id') == 'executions.wait' and isinstance(args.get('params'), dict)
              and type(args['params'].get('seconds', VPS_SECONDS)) is int
              and 1 <= args['params'].get('seconds', VPS_SECONDS) <= VPS_SECONDS):
            updated['params'] = {**args['params'], 'seconds': VPS_SECONDS}
        if updated != args:
            return {'hookEventName': event, 'permissionDecision': 'allow', 'updatedInput': updated}
        return None
    if event != 'PostToolUse':
        return None
    result = data.get('tool_response')
    if isinstance(result, dict) and isinstance(result.get('structuredContent'), dict):
        result = result['structuredContent']
    elif isinstance(result, dict) and isinstance(result.get('content'), list):
        blocks = result['content']
        if len(blocks) == 1 and blocks[0].get('type') == 'text':
            result = blocks[0].get('text')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {'output': result}
    if not isinstance(result, dict):
        return None
    message = None
    if name in ('Bash', 'exec_command', 'write_stdin') and result.get('exit_code') is None:
        session = result.get('session_id')
        if type(session) is not int:
            match = re.search(r'Process running with session ID (\d+)', str(result.get('output', '')))
            session = int(match[1]) if match else None
        if type(session) is int and session > 0:
            message = (f'Calcul local actif : attendre immédiatement la session {session} avec '
                       'write_stdin(chars="", yield_time_ms=300000). '
                       'Ne pas lire les logs ni commenter les compteurs entre deux attentes. '
                       'Si le code mode rend une cellule active, attendre cette même cellule avec '
                       'wait(yield_time_ms=900000). Aucun nouveau lancement. '
                       'À la fin, vérifier le code de sortie et les artefacts requis.')
    if (re.fullmatch(r'mcp__atelier[-_]routines__routines_run', name)
            and result.get('status') == 'running' and result.get('execution_id')):
        message = ('Calcul suivi actif : appeler directement executions.wait avec le même execution_id '
                   'et seconds=3600, sans sondage ni commentaire de progression. '
                   'À son échéance, conserver la référence et signaler la limite ; ne pas resoumettre.')
        if args.get('id') == 'executions.wait':
            message = ('Échéance de l’attente atteinte : calcul encore actif, aucune validation acquise. '
                       'Conserver execution_id et les preuves ; signaler cette limite sans relance automatique.')
    if (re.fullmatch(r'mcp__atelier[-_]vps__vps_(run|wait)', name)
            and result.get('status', result.get('state')) in ('running', 'queued') and result.get('job_id')):
        message = ('Calcul VPS actif : utiliser vps_wait sur le job_id connu avec timeout=3600. '
                   'Ne pas sonder vps_status ni commenter les compteurs. Garder la référence ; ne pas resoumettre.')
        if name.endswith('__vps_wait'):
            message = ('Échéance VPS atteinte : conserver le job_id, signaler l’état actif non validé ; '
                       'aucune relance ni libération automatique.')
    if message:
        return {'hookEventName': event, 'additionalContext': message}
    return None


def main():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        return
    try:
        data = json.loads(raw)
        output = response(data) if isinstance(data, dict) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return
    if output:
        print(json.dumps({'hookSpecificOutput': output}, ensure_ascii=False))


if __name__ == '__main__':
    main()
