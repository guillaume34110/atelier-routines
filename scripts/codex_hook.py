"""Static, bounded Codex reminders; never starts an operation or reads project data."""
import json
import re
import sys

RULES = ('Atelier routines: identifiant connu → routines_run; sinon une seule routines_find. '
         'CLI seulement si MCP indisponible. Une question seule ne lance aucune action. '
         'Conserver execution_id/request_id; inspecter ou reprendre la référence connue sans resoumettre. '
         'running/guided/unavailable/partial/error/timeout/decision_required/abandoned ne valident rien. '
         'Une preuve historique ne décrit pas nécessairement les faits actuels. Respecter les gardes et autorisations existantes.')


def reminder(data):
    event = data.get('hook_event_name')
    if event == 'SessionStart':
        return RULES
    if event != 'PostToolUse' or not re.fullmatch(r'mcp__atelier[-_]routines__routines_run', data.get('tool_name', '')):
        return None
    result = data.get('tool_response')
    if isinstance(result, dict) and isinstance(result.get('structuredContent'), dict):
        result = result['structuredContent']
    elif isinstance(result, dict) and isinstance(result.get('content'), list):
        blocks = result['content']
        if len(blocks) != 1 or blocks[0].get('type') != 'text':
            return None
        try:
            result = json.loads(blocks[0]['text'])
        except (ValueError, KeyError, TypeError):
            return None
    if not isinstance(result, dict):
        return None
    if result.get('status') in {'running', 'guided', 'unavailable', 'partial', 'error', 'timeout', 'decision_required', 'abandoned'}:
        return ('Ce retour ne valide pas la tâche. Utiliser les preuves et execution_id existants; '
                'ne pas relancer ni annoncer un succès. Une reprise attend ou collecte uniquement une opération connue.')
    if result.get('facts_current') is False:
        return 'Ce reçu est historique. Vérifier la fraîcheur avant de le présenter comme un fait actuel ou une validation.'
    return None


def main():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        return
    try:
        data = json.loads(raw)
        message = reminder(data) if isinstance(data, dict) else None
    except (ValueError, TypeError, AttributeError):
        return
    if message:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': data['hook_event_name'],
                                                'additionalContext': message}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
