"""Owned local Metal service or one isolated CPU recipe; no model downloads."""
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from decision_payload import MODEL_SHA, PROJECTOR_SHA, digest, require, summarize, validate


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0 and info.st_size < 32768, 'Fichier JSON privé non sûr.')
        raw = stream.read(32768)
        require(len(raw) < 32768, 'Fichier JSON privé trop volumineux.')
        return json.loads(raw)


def model_directory():
    return Path.home() / '.local/share/atelier-routines-models/d1'


def read_endpoint():
    endpoint = private_json(model_directory() / 'endpoint.json')
    require(type(endpoint) is dict and type(endpoint.get('port')) is int and 1024 <= endpoint['port'] <= 65535,
            'Port local invalide.')
    require(endpoint.get('model_sha256') == MODEL_SHA and type(endpoint.get('token')) is str, 'Service d1 incompatible.')
    return endpoint


def write_private(path, value):
    temporary = path.with_name(path.name+'.'+secrets.token_hex(6))
    fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(value,out)
    os.replace(temporary,path)


def api(port, token, payload=None, timeout=8):
    connection = http.client.HTTPConnection('127.0.0.1',port,timeout=timeout)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request('POST' if body is not None else 'GET','/v1/systemone' if body is not None else '/health',
                           body,{'Authorization':'Bearer '+token,'Content-Type':'application/json','Connection':'close'})
        response = connection.getresponse()
        require(response.status==200,'API d1 indisponible : HTTP '+str(response.status))
        raw = response.read(65537)
        require(len(raw)<=65536,'Réponse d1 trop volumineuse.')
        return json.loads(raw)
    finally:
        connection.close()


def verify_assets(config):
    for key,sha in (('model',MODEL_SHA),('projector',PROJECTOR_SHA),('binary',config['binary_sha256'])):
        path = Path(config[key])
        require(path.is_file() and not path.is_symlink(),'Fichier modèle/runtime absent ou symbolique.')
        with path.open('rb') as stream:
            require(hashlib.file_digest(stream,'sha256').hexdigest()==sha,'Empreinte modèle/runtime différente.')


@contextmanager
def server(config, directory, gpu):
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    verify_assets(config)
    token = secrets.token_urlsafe(32)
    key = directory/'api-key'
    write_private(key,token)
    # llama-server expects a plain key, never JSON quotes.
    key.write_text(token)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    logger = logging.getLogger('decision-runtime')
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(directory/'server.log',maxBytes=512*1024,backupCount=2,encoding='utf-8')
    logger.addHandler(handler)
    binary = config['binary']
    argv = [binary,'-m',config['model'],'--mmproj',config['projector'],'--host','127.0.0.1','--port',str(port),
            '--threads','4' if gpu else '2','--threads-batch','4' if gpu else '2','--parallel','1',
            '--ctx-size','16384','--batch-size','16384','--ubatch-size','16384','--cache-type-k','f32',
            '--cache-type-v','f32','--n-gpu-layers','99' if gpu else '0','--api-key-file',str(key)]
    env = dict(os.environ,DYLD_LIBRARY_PATH=str(Path(binary).parent)) if gpu else dict(os.environ,OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
    proc = subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env)
    def drain():
        for line in iter(lambda:proc.stdout.readline(8192),b''):
            logger.info(line.decode(errors='replace').rstrip())
    thread = threading.Thread(target=drain,daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+90
        while True:
            require(proc.poll() is None,'Serveur d1 arrêté au chargement ; consulter son journal privé.')
            try:
                if api(port,token,timeout=1).get('status')=='ok':
                    break
            except (OSError,ValueError):
                pass
            require(time.monotonic()<deadline,'Chargement d1 hors délai.')
            time.sleep(.2)
        yield port,token,proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        thread.join(timeout=3)
        proc.stdout.close()
        logger.removeHandler(handler)
        handler.close()
        key.unlink(missing_ok=True)


def serve(config_file):
    require(sys.platform=='darwin','Le service local requiert macOS/Metal.')
    config = private_json(config_file)
    directory = config_file.parent
    with (directory/'service.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def stop(signum,frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        try:
            with server(config,directory/'logs',True) as (port,token,proc):
                warm = {'state':'The parcel arrived Tuesday, not Monday.','questions':{'monday':{'type':'noul','instructions':'Did it arrive Monday?'}}}
                require(api(port,token,warm)['answers']['monday']['noul']<.5,'Canari d1 invalide.')
                write_private(directory/'endpoint.json',{'port':port,'token':token,'pid':proc.pid,'model_sha256':MODEL_SHA})
                proc.wait()
        finally:
            (directory/'endpoint.json').unlink(missing_ok=True)


def task():
    require(os.environ.get('USEVPS_JOB_ID') and sys.platform.startswith('linux'),'Tâche Atelier VPS requise.')
    os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:2])
    payload = validate(json.loads(Path('input.json').read_text()))
    config = json.loads(Path('model.json').read_text())
    require(set(config)=={'weights_job','runtime_job','threshold'},'Configuration de tâche inconnue.')
    require(all(re.fullmatch('[a-f0-9]{32}',config[k]) for k in ('weights_job','runtime_job')),'Référence modèle invalide.')
    root = Path('/workspace/jobs')
    model = root/config['weights_job']/'work/full-model'
    runtime = root/config['runtime_job']/'work/runtime'
    binaries = list(runtime.rglob('llama-server'))
    require(len(binaries)==1,'Runtime VPS conservé introuvable.')
    binary = binaries[0]
    runtime_archive = runtime/'runtime.tar.gz'
    with runtime_archive.open('rb') as stream:
        require(hashlib.file_digest(stream,'sha256').hexdigest()=='31cc3a6bc638ac6c48dc4b1b74888f1eaadf3d14227c1e5f67daf9a49541d97a','Archive runtime VPS différente.')
    executable = {'binary':str(binary),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
                  'model':str(model/'d1-2B-BF16.gguf'),'projector':str(model/'mmproj-d1-2B-BF16.gguf')}
    destination = Path('decision-result.json')
    require(not destination.exists(),'Résultat existant ; ne pas rejouer la tâche.')
    try:
        with server(executable,Path('decision-service'),False) as (port,token,proc):
            started = time.perf_counter()
            result = api(port,token,payload,timeout=120)
            summary = summarize(payload,result,config['threshold'],'vps_cpu_2',(time.perf_counter()-started)*1000)
        summary['server_closed'] = proc.poll() is not None
        destination.write_text(json.dumps(summary,ensure_ascii=False))
        print(json.dumps(summary,ensure_ascii=False))
    except Exception as error:
        destination.write_text(json.dumps({'status':'error','input_sha256':digest(payload),'message':str(error)[:250]}))
        raise


if __name__=='__main__':
    try:
        if sys.argv[1:]==['task']:
            task()
        elif len(sys.argv)==3 and sys.argv[1]=='serve':
            serve(Path(sys.argv[2]))
        else:
            raise ValueError('Mode de service/tâche invalide.')
    except KeyboardInterrupt:
        pass
