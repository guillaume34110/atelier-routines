"""Fixed local decision calls and explicit Atelier VPS task handoff."""
import base64
import hashlib
import json
import os
from pathlib import Path
import time

import executor as core
from decision_payload import MODEL_SHA, digest, require, summarize, validate
from decision_runtime import api, model_directory, private_json, read_endpoint


def route_candidates(query, entries):
    """Use the local model before asking the caller to disambiguate existing executable IDs."""
    if os.environ.get('ATELIER_ROUTINES_DECISION') == 'off' or len(entries)<2:
        return entries,None
    import routines
    terms = routines.words(query)
    if any(routines.words(e['id']) <= terms or query.strip().casefold() in
           [t.casefold() for t in e.get('triggers',[])] for e in entries):
        return entries,None
    candidates = [e for e in entries if core.availability(e)[0]=='executable' and len(e['id'].encode())<=48]
    if len(candidates)<2:
        return entries,None
    question = {'state':query,'questions':{'routine':{'type':'choice',
        'instructions':'Choose the existing routine that best matches the request. This only selects; execute nothing.',
        'criteria':{e['id']:e['description'].encode()[:256].decode(errors='ignore') for e in candidates}}}}
    validate(question)
    import usage
    with usage.capture('local-router','decision.evaluate',{'mode':'routine_selection'}) as observed:
        started = time.perf_counter()
        try:
            endpoint = read_endpoint()
            raw = api(endpoint['port'],endpoint['token'],question,timeout=2)
            selected = summarize(question,raw,.99,'mac_metal',(time.perf_counter()-started)*1000)
            observed.update(status='ok',result={'summary':{'mode':'routine_selection','usable':selected['usable']}})
            answer = selected['answers']['routine']
            info = {'backend':'d1-local','accepted':selected['usable'],'confidence':round(answer['confidence'],4),
                    'inference_ms':selected['inference_ms'],'fallback_required':not selected['usable']}
            if selected['usable']:
                entries = sorted(entries,key=lambda e:e['id']!=answer['value'])
                info['recommended_id'] = answer['value']
            return entries,info
        except (OSError,ValueError,KeyError,TypeError):
            observed.update(status='unavailable')
            return entries,{'backend':'d1-local','accepted':False,'fallback_required':True,'reason':'unavailable_or_invalid'}


def payload(project, file=None, request=None):
    require((file is not None) != (request is not None),'Choisir exactement file ou input.')
    require(request is None or type(request) is dict,'input doit être un objet JSON.')
    data = json.loads(core.read_file(project,file,65536)) if request is None else dict(request)
    require(type(data) is dict,'Objet de décision requis.')
    if 'state_file' in data:
        require('state' not in data and type(data['state_file']) is str,'Choisir state ou state_file.')
        data['state'] = core.read_file(project,data.pop('state_file'),24000)
    if 'images' in data:
        require(type(data['images']) is list and len(data['images'])<=1,'Une image maximum.')
        images = []
        for image in data['images']:
            require(type(image) is str,'Chemin image relatif requis.')
            raw = core.read_bytes(project,image,2*1024*1024)
            images.append('data:image/png;base64,'+base64.b64encode(raw).decode())
        data['images'] = images
    return validate(data)


def result(summary):
    # A low-confidence answer completes a read-only inference; never lock the whole project awaiting a decision.
    if 'usable' in summary:
        summary['fallback_required'] = not summary['usable']
    return {'summary':summary,'details':[],'partial':False,'internal_commands':0,'workflow_status':'ok'}


def prepare(project, data, threshold):
    # Fixed source/recipe only: the model can never supply commands or connector arguments.
    config = private_json(model_directory()/'config.json').get('vps')
    require(type(config) is dict and set(config)=={'weights_job','runtime_job'},'Références de poids VPS non configurées.')
    import re
    require(all(type(v) is str and re.fullmatch('[a-f0-9]{32}',v) for v in config.values()),'Références VPS invalides.')
    serialized = json.dumps(data,ensure_ascii=False,sort_keys=True)
    require(core.redact(serialized)==serialized,'Secret détecté : aucun paquet VPS préparé.')
    files = {name:Path(__file__).with_name(name).read_bytes() for name in ('decision_runtime.py','decision_payload.py')}
    files['input.json'] = serialized.encode()
    files['model.json'] = json.dumps({**config,'threshold':threshold}).encode()
    files['.atelier-vps.json'] = json.dumps({'version':1,'id':'routines-d1','recipes':{'decision':{
        'argv':['rtk','proxy','python3','decision_runtime.py','task'],'platform':'linux','arch':'x86_64',
        'artifacts':['decision-result.json']}}}).encode()
    signature = hashlib.sha256(b''.join(k.encode()+files[k] for k in sorted(files))).hexdigest()
    parent = project/'output/routines-decision-tasks'
    with core.directory_fd(parent,create=True):
        existing = list(parent.iterdir())
        require(len(existing)<64 or (parent/signature).exists(),'Quota64 paquets atteint ; préserver puis nettoyer les paquets inutilisés.')
        used = sum(p.stat().st_size for d in existing if d.is_dir() and not d.is_symlink()
                   for p in d.iterdir() if p.is_file() and not p.is_symlink())
        require(used+sum(map(len,files.values()))<=64*1024*1024,'Budget64 Mio de paquets atteint.')
    directory = parent/signature
    with core.directory_fd(directory,create=True) as fd:
        for name,content in files.items():
            target = directory/name
            if target.exists() or target.is_symlink():
                require(core.read_bytes(project,str(target.relative_to(project)))==content,'Paquet précédent différent ; préservé.')
                continue
            handle = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(handle,'wb') as out:
                out.write(content)
    return result({'prepared':True,'inference_submitted':False,'input_sha256':digest(data),
        'tool':'mcp__atelier_vps__vps_task','arguments':{'source':str(directory),'recipe':'decision',
        'agent':'decision-'+signature[:12],'server':'hetzner-preprod','arch':'x86_64','cpus':2,
        'ram_gib':10,'disk_gib':.2,'timeout':300},'fetch':'work/decision-result.json',
        'next':'Appeler vps_task, puis vps_fetch ; decision.result lit le résultat. Préserver les preuves avant vps_release.'})


def execute(identifier, project, params):
    if identifier=='decision.status':
        try:
            endpoint = read_endpoint()
            require(api(endpoint['port'],endpoint['token'],timeout=2).get('status')=='ok','Service non prêt.')
        except (OSError,ValueError) as error:
            raise core.Refusal('d1 local indisponible : '+str(error)[:120], 'unavailable') from None
        return result({'backend':'mac_metal','model':'d1-2B-BF16','ready':True,'pid':endpoint['pid'],
                       'cloud_tokens_used':0,'service':'fr.atelier.routines.d1'})
    if identifier=='decision.result':
        saved = json.loads(core.read_file(project,params['file'],16384))
        original = json.loads(core.read_file(project,params['request'],2800000))
        validate(original)
        require(saved.get('input_sha256')==digest(original) and saved.get('model_sha256')==MODEL_SHA,
                'Le résultat ne correspond pas au paquet de décision.')
        require(saved.get('backend')=='vps_cpu_2' and saved.get('server_closed') is True,'Résultat VPS incomplet.')
        require(type(saved.get('answers')) is dict and set(saved['answers'])==set(original['questions']), 'Questions du résultat incompatibles.')
        # Reconstruct native fields to apply the same output checks to an imported artifact.
        native = {'answers':{}}
        for name,q in original['questions'].items():
            a = saved['answers'][name]
            if q['type']=='choice':
                native['answers'][name] = {'choice':a['value'],'confidence':a['confidence']}
            else:
                require(type(a['value']) is bool,'Valeur binaire invalide.')
                native['answers'][name] = {'noul':a['confidence'] if a['value'] else 1-a['confidence']}
        checked = summarize(original,native,params['min_confidence'],'vps_cpu_2',saved['inference_ms'])
        checked['server_closed'] = True
        checked['source'] = 'artefact récupéré ; ne soumet aucune inférence'
        return result(checked)
    data = payload(project,params.get('file'),params.get('input'))
    if identifier=='decision.prepare-vps':
        return prepare(project,data,params['min_confidence'])
    started = time.perf_counter()
    try:
        endpoint = read_endpoint()
        raw = api(endpoint['port'],endpoint['token'],data)
    except (OSError,ValueError) as error:
        raise core.Refusal('Inférence d1 indisponible ; aucune relance ni bascule implicite : '+str(error)[:100],'unavailable') from None
    return result(summarize(data,raw,params['min_confidence'],'mac_metal',(time.perf_counter()-started)*1000))
