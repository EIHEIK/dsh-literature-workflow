import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,digest
from validate_mailbox_container import wsl_path

docker=[str(Path(os.environ['WINDIR'])/'System32/wsl.exe'),'-d','Ubuntu-22.04','--','docker']
record=json.loads(subprocess.check_output([*docker,'image','inspect','literature-engine:2.10.2-0.7.1'],encoding='utf-8'))[0]
image=record['Id']
case=ROOT/'.test-output'/('engine-image-'+time.strftime('%Y%m%d-%H%M%S'));case.mkdir(parents=True)
command=[*docker,'run','--rm','--pull','never','--name','lit-engine-validation-'+secrets.token_hex(4),
         '--network','none','--read-only','--user','10001:10001','--cap-drop','ALL',
         '--security-opt','no-new-privileges','--pids-limit','256','--memory','3g','--cpus','2',
         '--tmpfs','/tmp:rw,noexec,nosuid,size=128m','-e','HOME=/tmp/literature-home',
         '-e','XDG_CACHE_HOME=/tmp/literature-home/.cache',
         '--mount',f'type=bind,source={wsl_path(ROOT/"scripts/engine_container_probe.py")},target=/app/probe.py,readonly',
         '--mount',f'type=bind,source={wsl_path(case)},target=/work',image,'python','/app/probe.py']
with open(case/'docker.log','wb') as log:
    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=180,creationflags=0x08000000)
if result.returncode:raise RuntimeError('Image validation failed; see '+str(case/'docker.log'))
report=json.loads((case/'validation.json').read_text(encoding='utf-8'))
if not report['passed']:raise ValueError('Image validation did not pass')
lock={'imageId':image,
      'localTag':'literature-engine:2.10.2-0.7.1','created':record['Created'],
      'requirementsSha256':digest(ROOT/'deploy/engine-requirements.lock'),
      'dockerfileSha256':digest(ROOT/'deploy/Dockerfile.engine'),'imageSizeBytes':record['Size'],
      'validation':report,'note':'Local image, not published. Runtime must use imageId with --pull never.'}
atomic(ROOT/'deploy/engine-image.lock.json',lock)
print(json.dumps({'evidence':str(case/'validation.json'),'imageId':image,'passed':report['passed']},ensure_ascii=False))
