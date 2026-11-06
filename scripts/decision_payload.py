"""Bounded d1 decision contract shared by Mac and Atelier VPS tasks."""
import base64
import hashlib
import json
import math
import struct

MODEL_SHA = 'f785c92583aa83ad2ecccd88d222a5b6b81ba1e66cb1465e7747eb9f857c67c2'
PROJECTOR_SHA = '67478334d694f13fd04f51ca708dd2410953c499b0201a7482f449eb471b88f3'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, maximum):
    return type(value) is str and 0 < len(value.encode()) <= maximum and '\0' not in value


def validate(payload):
    require(type(payload) is dict and not set(payload)-{'state','questions','images'}, 'Champs de décision inconnus.')
    require(text(payload.get('state'),24000), 'state doit contenir 1 à24000 octets UTF-8.')
    questions = payload.get('questions')
    require(type(questions) is dict and 1 <= len(questions) <= 4, 'Fournir une à quatre questions.')
    for name,q in questions.items():
        require(text(name,32) and type(q) is dict, 'Question invalide.')
        require(q.get('type') in ('choice','noul') and text(q.get('instructions'),512), 'Types autorisés : choice/noul, avec instructions.')
        fields = {'type','instructions','criteria'} if q['type']=='choice' else {'type','instructions'}
        require(set(q)==fields, 'Champs de question inconnus ou manquants.')
        if q['type']=='choice':
            choices = q['criteria']
            require(type(choices) is dict and 2 <= len(choices) <= 8, 'Deux à huit choix requis.')
            require(all(text(k,48) and text(v,256) for k,v in choices.items()), 'Choix trop longs ou invalides.')
    images = payload.get('images',[])
    require(type(images) is list and len(images)<=1, 'Une image PNG maximum.')
    for image in images:
        require(type(image) is str and image.startswith('data:image/png;base64,') and len(image)<2800000, 'Image PNG de2 Mio maximum.')
        raw = base64.b64decode(image.split(',',1)[1],validate=True)
        require(len(raw)>=33 and raw[:8]==b'\x89PNG\r\n\x1a\n' and raw[12:16]==b'IHDR', 'En-tête PNG invalide.')
        width,height = struct.unpack('>II',raw[16:24])
        require(0<width<=2048 and 0<height<=2048, 'Dimensions PNG limitées à2048×2048.')
    return payload


def digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def summarize(payload, result, threshold, backend, elapsed):
    require(type(threshold) in (int,float) and math.isfinite(threshold) and .9<=threshold<=1, 'Seuil invalide.')
    raw = result.get('answers') if type(result) is dict else None
    require(type(raw) is dict and set(raw)==set(payload['questions']), 'Réponse d1 incomplète.')
    answers = {}
    for name,q in payload['questions'].items():
        a = raw[name]
        require(type(a) is dict, 'Réponse structurée requise.')
        p = a.get('confidence') if q['type']=='choice' else a.get('noul')
        require(type(p) in (int,float) and math.isfinite(p) and 0<=p<=1, 'Confiance invalide.')
        value = a.get('choice') if q['type']=='choice' else p>=.5
        require(q['type']!='choice' or type(value) is str and value in q['criteria'], 'Choix hors liste autorisée.')
        confidence = p if q['type']=='choice' else max(p,1-p)
        answers[name] = {'value':value,'confidence':confidence,'accepted':confidence>=threshold}
    return {'backend':backend,'model':'d1-2B-BF16','model_sha256':MODEL_SHA,'input_sha256':digest(payload),
            'answers':answers,'usable':all(a['accepted'] for a in answers.values()),'min_confidence':threshold,
            'inference_ms':round(elapsed,2),'cloud_tokens_used':0,
            'scope':'Décision seulement ; aucune action exécutée, aucune permission accordée.'}
