"""Install the existing verified BF16 assets as an explicit per-user LaunchAgent."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

from decision_runtime import private_json, verify_assets, write_private

LABEL = 'fr.atelier.routines.d1'


def install(assets, weights_job=None, runtime_job=None, start=True):
    if sys.platform!='darwin' or os.uname().machine!='arm64':
        raise ValueError('Service BF16 Metal réservé à un Mac Apple Silicon.')
    assets = assets.resolve(strict=True)
    manifest = json.loads((assets/'manifest.json').read_text())
    binary = (assets/manifest['binary']).resolve(strict=True)
    if not binary.is_relative_to(assets):
        raise ValueError('Runtime hors des assets vérifiés.')
    config = {'binary':str(binary),'binary_sha256':manifest['local_build']['binary_sha256'],
              'model':str(assets/'d1-2B-BF16.gguf'),'projector':str(assets/'mmproj-d1-2B-BF16.gguf')}
    verify_assets(config)
    if bool(weights_job)!=bool(runtime_job):
        raise ValueError('Les deux références de poids/runtime VPS sont requises ensemble.')
    if weights_job:
        if not all(re.fullmatch('[a-f0-9]{32}',v) for v in (weights_job,runtime_job)):
            raise ValueError('Référence VPS invalide.')
        config['vps'] = {'weights_job':weights_job,'runtime_job':runtime_job}
    directory = Path.home()/'.local/share/atelier-routines-models/d1'
    if directory.is_symlink():
        raise ValueError('Dossier de modèle symbolique interdit.')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.chmod(0o700)
    config_file = directory/'config.json'
    if config_file.exists() and private_json(config_file)!=config:
        raise ValueError('Installation différente déjà présente ; configuration préservée.')
    if not config_file.exists():
        write_private(config_file,config)
    runtime = Path(__file__).resolve().with_name('decision_runtime.py')
    rtk = shutil.which('rtk')
    if not rtk:
        raise ValueError('rtk requis.')
    agent = {'Label':LABEL,'ProgramArguments':[rtk,'proxy',sys.executable,str(runtime),'serve',str(config_file)],
             'RunAtLoad':True,'KeepAlive':False,'ProcessType':'Background',
             'StandardOutPath':str(directory/'service.stdout.log'),'StandardErrorPath':str(directory/'service.stderr.log')}
    agents = Path.home()/'Library/LaunchAgents'
    agents.mkdir(exist_ok=True)
    target = agents/(LABEL+'.plist')
    content = plistlib.dumps(agent)
    if target.is_symlink() or target.exists() and target.read_bytes()!=content:
        raise ValueError('LaunchAgent différent déjà présent ; aucun remplacement.')
    if not target.exists():
        fd = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as out:
            out.write(content)
    if start:
        scope = 'gui/'+str(os.getuid())
        loaded = subprocess.run(['rtk','proxy','launchctl','print',scope+'/'+LABEL],capture_output=True)
        if loaded.returncode:
            subprocess.run(['rtk','proxy','launchctl','bootstrap',scope,str(target)],check=True)
        elif not (directory/'endpoint.json').exists():
            subprocess.run(['rtk','proxy','launchctl','kickstart',scope+'/'+LABEL],check=True)
    print(json.dumps({'service':LABEL,'installed':True,'start_requested':start,'assets_copied':False,
                      'config':str(config_file),'launch_agent':str(target),'keep_alive':False}))


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--vps-weights-job')
    parser.add_argument('--vps-runtime-job')
    parser.add_argument('--no-start',action='store_true')
    args = parser.parse_args()
    install(args.assets,args.vps_weights_job,args.vps_runtime_job,not args.no_start)
