"""Fixed GitHub operations with repository-bound credentials; never switch accounts."""
import asyncio
import json
import re
import time
from urllib.parse import quote

ACCOUNTS = {'guillaume34110': 'guillaume34110', 'guillaume-rebaze': 'guillaume-rebaze'}
WRITES = {'git.pr-create', 'git.merge'}
REMOTE = {'type': 'string', 'minLength': 1, 'maxLength': 100, 'default': 'origin'}
NUMBER = {'type': 'integer', 'minimum': 1, 'maximum': 2147483647}
SHA = {'type': 'string', 'minLength': 40, 'maxLength': 64}
WAIT = {'type': 'integer', 'minimum': 0, 'maximum': 3600, 'default': 0}
def schemas(schema):
    return {
        'github.context': schema(remote=REMOTE),
        'github.prs': schema(remote=REMOTE, scope={'type': 'string', 'enum': ['project', 'accounts'], 'default': 'project'},
                              page={'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 1},
                              limit={'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}),
        'git.protection': schema(remote=REMOTE, base={'type': 'string', 'maxLength': 200, 'default': ''}),
        'git.pr-create': schema(remote=REMOTE, branch={'type': 'string', 'minLength': 1, 'maxLength': 200},
                                 base={'type': 'string', 'maxLength': 200, 'default': ''}, head_sha=SHA,
                                 title={'type': 'string', 'minLength': 1, 'maxLength': 240},
                                 body_file={'type': 'string', 'maxLength': 1024, 'default': ''}),
        'git.pr-checks': schema(remote=REMOTE, number=NUMBER, head_sha=SHA, wait=WAIT),
        'git.integration': schema(remote=REMOTE, number=NUMBER, head_sha=SHA, wait=WAIT),
        'git.merge': schema(remote=REMOTE, number=NUMBER, head_sha=SHA,
                             method={'type': 'string', 'enum': ['merge', 'squash', 'rebase'], 'default': 'squash'}),
    }


import executor as core


class UncertainWrite(core.Refusal):
    effect_uncertain = True

    def __init__(self, message):
        super().__init__(message, 'decision_required')


PR_FIELDS = '{number,title,html_url,state,merged,draft,head:{sha:.head.sha,ref:.head.ref,repo:{full_name:.head.repo.full_name}},base:{ref:.base.ref,sha:.base.sha,repo:{full_name:.base.repo.full_name}}}'


def config(project, key):
    code, text, limited = core.capture(['rtk', 'proxy', 'git', 'config', '--local', '--get-all', key], project)
    if limited or code not in (0, 1):
        raise core.Refusal('Configuration Git locale illisible.')
    return text.strip().splitlines() if code == 0 else []


def account_for(owner, configured=()):
    expected = ACCOUNTS.get(owner.casefold())
    if len(configured) > 1 or (configured and configured[0] not in ACCOUNTS):
        raise core.Refusal('atelier.githubAccount doit nommer un seul des deux comptes connus.', 'decision_required')
    if expected and configured and configured[0] != expected:
        raise core.Refusal('Compte configuré incompatible avec le propriétaire ' + owner + ' ; attendu : ' + expected + '.', 'decision_required')
    if not expected and not configured:
        raise core.Refusal('Propriétaire inconnu : associer ce dépôt une fois avec git config --local atelier.githubAccount COMPTE.', 'decision_required')
    return expected or configured[0]


def token_for(project, account):
    code, text, limited = core.capture(['rtk', 'proxy', 'gh', 'auth', 'token', '--hostname', 'github.com', '--user', account],
                                       project, limit=4096)
    token = text.strip()
    if code or limited or not token or any(c.isspace() for c in token):
        raise core.Refusal('Connexion GitHub manquante ou expirée pour ' + account + ' ; connecter ce compte avec gh auth login --hostname github.com.', 'unavailable')
    return token


class Client:
    def __init__(self, project, account):
        self.project, self.account = project, account
        self._token = token_for(project, account)
        user = self.get('user', jq='{login}')
        if user.get('login', '').casefold() != account:
            raise core.Refusal('Identité GitHub différente du compte attendu : ' + account + '.', 'decision_required')

    def request(self, endpoint, method='GET', data=None, jq=None, optional=False):
        argv = ['rtk', 'proxy', 'gh', 'api', '--hostname', 'github.com', '--method', method, endpoint]
        if jq:
            argv += ['--jq', jq]
        if data is not None:
            argv += ['--input', '-']
        try:
            code, text, limited = core.capture(argv, self.project, timeout=20,
                                               payload=json.dumps(data).encode() if data is not None else None,
                                               credentials={'GH_TOKEN': self._token})
        except (core.Refusal, OSError) as error:
            if method != 'GET':
                raise UncertainWrite('Réponse GitHub incertaine ; reprendre cette exécution pour vérifier l’effet distant, sans réécrire.') from None
            raise error
        if code or limited:
            found = re.search(r'HTTP (\d{3})', text)
            status = int(found[1]) if found else None
            if optional and status in (403, 404):
                return None
            if method != 'GET' and (status is None or status >= 500):
                raise UncertainWrite('Réponse GitHub incertaine ; reprendre cette exécution, sans nouvelle soumission.')
            raise core.Refusal('GitHub ' + str(status or 'indisponible') + ' pour ' + self.account + ' ; aucune autre identité essayée.',
                               'decision_required' if status in (409, 422) else 'unavailable')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if method != 'GET':
                raise UncertainWrite('Réponse d’écriture non JSON ; vérifier l’effet distant, sans nouvelle soumission.') from None
            raise core.Refusal('Réponse GitHub non JSON ; aucune validation acquise.', 'decision_required') from None

    def get(self, endpoint, **kwargs):
        return self.request(endpoint, **kwargs)


def repository(project, remote):
    entries = [r for r in core.git_remotes(project) if r['name'] == remote]
    if not entries:
        raise core.Refusal('Remote Git absent : ' + remote + '.')
    repos = {'fetch': set(), 'push': set()}
    for entry in entries:
        host = entry['host']
        if host != 'github.com' and entry['transport'] == 'ssh' and host:
            code, text, limited = core.capture(['rtk', 'proxy', 'ssh', '-G', host], project)
            names = [l.split(None, 1)[1] for l in text.splitlines() if l.startswith('hostname ')]
            host = names[0] if code == 0 and not limited and len(names) == 1 else None
        name = entry['repository'].removesuffix('.git')
        if host != 'github.com' or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name):
            raise core.Refusal('Remote non résolu vers github.com ; aucune credential transmise.', 'unavailable')
        repos[entry['direction']].add(name)
    if len(repos['fetch']) != 1:
        raise core.Refusal('Destination de lecture ambiguë.', 'decision_required')
    return next(iter(repos['fetch'])), sorted(repos['push'])


def context(project, remote):
    name, pushes = repository(project, remote)
    account = account_for(name.split('/')[0], config(project, 'atelier.githubAccount'))
    client = Client(project, account)
    repo = client.get('repos/' + name, jq='{full_name,default_branch,permissions}')
    if repo.get('full_name', '').casefold() != name.casefold():
        raise core.Refusal('Le dépôt GitHub a changé de propriétaire ou de nom ; vérifier le remote.', 'decision_required')
    info = {'repository': name, 'account': account, 'default_branch': repo['default_branch'],
            'permissions': repo.get('permissions') or {}, 'push_repositories': pushes,
            'push_target_matches': len(pushes) == 1 and pushes[0].casefold() == name.casefold(),
            'git_transport_verified': False}
    return client, info


def branch(project, value):
    if not value or value.startswith('-'):
        raise core.Refusal('Nom de branche invalide.')
    core.git(project, 'check-ref-format', '--branch', value)
    return quote(value, safe='')


def sha(value):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
        raise core.Refusal('head_sha doit être une empreinte Git complète.')
    return value


def pull(client, info, number, expected):
    item = client.get('repos/' + info['repository'] + '/pulls/' + str(number), jq=PR_FIELDS)
    if item['base']['repo']['full_name'].casefold() != info['repository'].casefold() or item['head']['sha'] != sha(expected):
        raise core.Refusal('Dépôt ou SHA de PR différent ; relire la révision courante.', 'decision_required')
    return item


def protections(client, info, base):
    target = branch(client.project, base)
    prefix = 'repos/' + info['repository']
    classic = client.get(prefix + '/branches/' + target + '/protection', optional=True)
    rules = client.get(prefix + '/rules/branches/' + target)
    required, sources, pr, strict, admins = set(), [], False, False, False
    if classic is not None:
        checks = classic.get('required_status_checks') or {}
        required.update(checks.get('contexts', []))
        required.update(c['context'] for c in checks.get('checks', []))
        sources.extend({'context': c['context'], 'app_id': c.get('app_id')} for c in checks.get('checks', []))
        pr = classic.get('required_pull_request_reviews') is not None
        strict = checks.get('strict') is True
        admins = (classic.get('enforce_admins') or {}).get('enabled') is True
    rulesets = {}
    for rule in rules:
        rid = rule.get('ruleset_id')
        if rid is not None and rid not in rulesets:
            if len(rulesets) >= 10:
                raise core.Refusal('Trop de rulesets ; contrôle incomplet.', 'partial')
            rulesets[rid] = client.get(prefix + '/rulesets/' + str(int(rid)), optional=True)
        definition = rulesets.get(rid)
        enforced = definition is not None and definition.get('enforcement') == 'active' and definition.get('bypass_actors') == []
        params = rule.get('parameters') or {}
        if rule['type'] == 'pull_request' and enforced:
            pr = True
        if rule['type'] == 'required_status_checks':
            required.update(c['context'] for c in params.get('required_status_checks', []))
            sources.extend({'context': c['context'], 'app_id': c.get('integration_id')} for c in params.get('required_status_checks', []))
            if enforced and params.get('strict_required_status_checks_policy') is True:
                strict = True
        if enforced and rule['type'] in ('pull_request', 'required_status_checks'):
            admins = True if classic is None else admins
    required.update(config(client.project, 'atelier.requiredCheck'))
    if info['repository'].casefold() == 'guillaume34110/atelier-routines':
        required.update(('verify', 'security'))
    reasons = []
    for valid, reason in [(pr, 'PR obligatoire non vérifiée'), (strict, 'branche à jour non imposée'),
                          (admins, 'application aux administrateurs non vérifiée'), (bool(required), 'contrôles requis absents')]:
        if not valid:
            reasons.append(reason)
    return {'base': base, 'required_checks': sorted(required), 'pull_request_required': pr,
            'required_sources': sources, 'strict': strict, 'administrators_enforced': admins, 'verified': not reasons, 'reasons': reasons}


def checks(client, info, item, policy):
    prefix = 'repos/' + info['repository'] + '/commits/' + item['head']['sha']
    runs = client.get(prefix + '/check-runs?per_page=100', jq='{total_count,check_runs:[.check_runs[]|{id,name,status,conclusion,head_sha,app_id:.app.id}]}')
    statuses = client.get(prefix + '/status?per_page=100', jq='{total_count,statuses:[.statuses[]|{id,context,state}]}')
    if runs['total_count'] > 100 or statuses['total_count'] > 100:
        raise core.Refusal('Plus de 100 contrôles ou statuts ; vérification incomplète.', 'partial')
    latest = {}
    for run in sorted(runs['check_runs'], key=lambda r: r['id']):
        if run.get('head_sha') != item['head']['sha']:
            raise core.Refusal('Contrôle rattaché à un autre SHA.', 'decision_required')
        latest[(run['name'], run.get('app_id'))] = {'name': run['name'], 'state': run['conclusion'] if run['status'] == 'completed' else 'pending'}
    for status in sorted(statuses['statuses'], key=lambda s: s['id']):
        latest[(status['context'], 'status')] = {'name': status['context'], 'state': status['state']}
    rows = list(latest.values())
    missing = sorted(set(policy['required_checks']) - {r['name'] for r in rows})
    for source in policy['required_sources']:
        app_id = source['app_id']
        if app_id not in (None, -1) and (source['context'], app_id) not in latest:
            missing.append(source['context'] + ' (application ' + str(app_id) + ')')
    failed = [r for r in rows if r['state'] not in ('success', 'pending')]
    pending = [r for r in rows if r['state'] == 'pending']
    return {'head_sha': item['head']['sha'], 'missing': missing, 'failed': failed, 'pending': pending,
            'passed': bool(rows) and not (missing or failed or pending), 'checks': rows}


def gate(client, info, params, wait_seconds=0):
    deadline = time.monotonic() + wait_seconds
    while True:
        item = pull(client, info, params['number'], params['head_sha'])
        policy = protections(client, info, item['base']['ref'])
        observed = checks(client, info, item, policy)
        if not observed['pending'] or observed['failed'] or observed['missing'] or time.monotonic() >= deadline:
            break
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    comparison = client.get('repos/' + info['repository'] + '/compare/' + branch(client.project, item['base']['ref']) + '...' + sha(params['head_sha']),
                            jq='{behind_by,base_sha:.base_commit.sha}')
    reasons = [*policy['reasons']]
    if not observed['passed']:
        reasons.append('contrôles absents, en attente ou non verts')
    if comparison['behind_by'] != 0:
        reasons.append('branche en retard sur sa cible')
    if item['state'] != 'open' or item['draft']:
        reasons.append('PR fermée ou brouillon')
    return item, {'repository': info['repository'], 'account': info['account'], 'number': params['number'],
                  'head_sha': params['head_sha'], 'ready': not reasons, 'reasons': reasons,
                  'protection': policy, 'checks': observed, 'base_sha': comparison['base_sha']}


def redact_values(value):
    if isinstance(value, str):
        return core.redact(value)
    if isinstance(value, list):
        return [redact_values(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_values(item) for key, item in value.items()}
    return value


def result(summary, details=(), status='ok'):
    return {'summary': redact_values(summary), 'details': redact_values(list(details)), 'partial': status == 'partial',
            'workflow_status': status, 'internal_commands': core.COMMANDS, 'inputs': list(core.READS.values())}


def writable(info):
    if not info['push_target_matches']:
        raise core.Refusal('Destinations de lecture et de poussée différentes ou multiples ; écriture refusée.', 'decision_required')
    if not info['permissions'].get('push'):
        raise core.Refusal('Le compte ' + info['account'] + ' n’a pas de droit d’écriture sur ' + info['repository'] + '.', 'unavailable')


def create(client, info, params, reconcile):
    writable(info)
    head = branch(client.project, params['branch'])
    base = params['base'] or info['default_branch']
    branch(client.project, base)
    if params['branch'] in {base, info['default_branch'], 'main', 'master'}:
        raise core.Refusal('Utiliser une branche de travail, distincte de la cible.')
    ref = client.get('repos/' + info['repository'] + '/git/ref/heads/' + head, jq='{sha:.object.sha}')
    if ref['sha'] != sha(params['head_sha']):
        raise core.Refusal('Branche distante absente ou sur un autre SHA ; publier la branche après les validations du projet.', 'decision_required')
    endpoint = 'repos/' + info['repository'] + '/pulls?state=all&head=' + quote(info['repository'].split('/')[0] + ':' + params['branch'], safe='') + '&base=' + quote(base, safe='') + '&per_page=100'
    matches = client.get(endpoint, jq='map(' + PR_FIELDS + ')')
    matches = [p for p in matches if p['head']['sha'] == params['head_sha'] and p['head']['ref'] == params['branch']
               and p['base']['ref'] == base and p['head']['repo']['full_name'].casefold() == info['repository'].casefold()]
    if len(matches) == 1:
        return result({'repository': info['repository'], 'account': info['account'], 'number': matches[0]['number'],
                       'url': matches[0]['html_url'], 'created': False, 'state': matches[0]['state']})
    if matches or reconcile:
        raise core.Refusal('Effet distant non établi ; aucune nouvelle création soumise.', 'decision_required')
    body = core.read_file(client.project, params['body_file']) if params['body_file'] else ''
    if len(body.encode()) > 16000:
        raise core.Refusal('Corps de PR limité à 16 000 octets.')
    created = client.request('repos/' + info['repository'] + '/pulls', 'POST',
                             {'head': params['branch'], 'base': base, 'title': params['title'], 'body': body}, jq=PR_FIELDS)
    if created['head']['sha'] != params['head_sha'] or created['base']['repo']['full_name'].casefold() != info['repository'].casefold():
        raise UncertainWrite('PR créée sur une révision différente ; inspecter la PR, sans nouvelle soumission.')
    return result({'repository': info['repository'], 'account': info['account'], 'number': created['number'], 'url': created['html_url'], 'created': True})


def execute(identifier, project, params, runtime=None):
    runtime = runtime or {}
    if identifier == 'github.prs' and params['scope'] == 'accounts':
        rows, missing, pages = [], [], []
        for account in ACCOUNTS:
            try:
                client = Client(project, account)
                page = client.get('search/issues?q=' + quote('is:pr is:open user:' + account, safe='') + '&per_page=' + str(params['limit']) + '&page=' + str(params['page']),
                                  jq='{total_count,incomplete_results,items:[.items[]|{number,title,html_url,repository_url}]}')
                rows.extend({'account': account, **row} for row in page['items'])
                pages.append({'account': account, 'total': page['total_count'],
                              'has_more': params['page'] * params['limit'] < page['total_count']})
                if page['incomplete_results']:
                    missing.append({'account': account, 'reason': 'recherche incomplète'})
            except core.Refusal as error:
                missing.append({'account': account, 'reason': str(error), 'status': error.status})
        return result({'accounts': list(ACCOUNTS), 'returned': len(rows), 'page': params['page'], 'limit_per_account': params['limit'],
                       'pages': pages, 'missing_accounts': missing, 'scope': 'PR ouvertes des dépôts possédés par les deux comptes'}, rows, 'partial' if missing else 'ok')
    client, info = context(project, params['remote'])
    if identifier == 'github.context':
        return result(info)
    if identifier == 'github.prs':
        rows = client.get('repos/' + info['repository'] + '/pulls?state=open&per_page=' + str(params['limit']) + '&page=' + str(params['page']), jq='map(' + PR_FIELDS + ')')
        return result({'repository': info['repository'], 'account': info['account'], 'returned': len(rows),
                       'page': params['page'], 'has_more': len(rows) == params['limit']}, rows)
    if identifier == 'git.protection':
        policy = protections(client, info, params['base'] or info['default_branch'])
        return result({'repository': info['repository'], 'account': info['account'], **policy}, status='ok' if policy['verified'] else 'partial')
    if identifier == 'git.pr-create':
        return create(client, info, params, runtime.get('reconcile', False))
    if identifier == 'git.merge':
        writable(info)
        item = pull(client, info, params['number'], params['head_sha'])
        if item.get('merged'):
            return result({'repository': info['repository'], 'account': info['account'], 'number': params['number'], 'merged': True, 'already_merged': True})
        if runtime.get('reconcile'):
            raise core.Refusal('Fusion non constatée ; aucune nouvelle fusion soumise.', 'decision_required')
    item, observed = gate(client, info, params, runtime.get('wait_seconds', params.get('wait', 0)))
    if identifier != 'git.merge':
        passed = observed['checks']['passed'] if identifier == 'git.pr-checks' else observed['ready']
        state = 'ok' if passed else 'partial' if observed['checks']['pending'] and not (observed['checks']['failed'] or observed['checks']['missing']) else 'error'
        return result(observed, observed['checks']['checks'], state)
    if not observed['ready']:
        return result(observed, status='error')
    current = pull(client, info, params['number'], params['head_sha'])
    if current['base']['sha'] != item['base']['sha'] or current['base']['ref'] != item['base']['ref']:
        raise core.Refusal('Base de PR modifiée ; validation précédente invalidée.', 'decision_required')
    merged = client.request('repos/' + info['repository'] + '/pulls/' + str(params['number']) + '/merge', 'PUT',
                            {'sha': params['head_sha'], 'merge_method': params['method']}, jq='{merged,sha}')
    if merged.get('merged') is not True:
        raise UncertainWrite('GitHub n’a pas confirmé la fusion.')
    return result({'repository': info['repository'], 'account': info['account'], 'number': params['number'],
                   'head_sha': params['head_sha'], 'merged': True, 'merge_sha': merged['sha']})


async def continue_execution(project, record, seconds=0, reconcile=False):
    import executions
    deadline = time.monotonic() + seconds
    while record['state'] in executions.ACTIVE and executions.alive(record.get('owner_pid')):
        if time.monotonic() >= deadline:
            return core.compact(executions.view(record))
        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
        record = executions.get(record['execution_id'], project)
    if record['state'] in executions.ACTIVE:
        if not reconcile:
            return core.compact(executions.view(record))
    if not reconcile and (record['id'] not in ('git.pr-checks', 'git.integration') or record['state'] != 'partial'):
        return core.compact(executions.verified_view(record))
    return await asyncio.to_thread(core.run, record['id'], record['params'], record['execution_id'],
                                   max(0, int(deadline - time.monotonic())), reconcile)
