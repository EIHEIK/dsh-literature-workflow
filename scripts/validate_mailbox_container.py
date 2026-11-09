"""Exercise real Docker + Windows file IPC without a provider or model request."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from api_mailbox import MailboxInbox
from core import atomic


def wsl_path(path):
    path=Path(path).resolve(strict=True)
    if len(path.drive)!=2 or path.drive[1]!=':': raise ValueError('Expected local drive path')
    return '/mnt/'+path.drive[0].lower()+'/'+path.as_posix()[3:]


def main():
    case=ROOT/'.test-output'/('mailbox-container-'+time.strftime('%Y%m%d-%H%M%S'))
    ipc=case/'ipc';work=case/'work';ipc.mkdir(parents=True);work.mkdir()
    credential=secrets.token_urlsafe(32);atomic(case/'fixture.json',{'credential':credential})
    inbox=MailboxInbox(ipc,credential,1)
    lock=json.loads((ROOT/'deploy/base-image.lock.json').read_text(encoding='utf-8'))
    image='docker.io/library/python@'+lock['digest']
    name='lit-mailbox-fixture-'+secrets.token_hex(5)
    docker=[str(Path(os.environ['WINDIR'])/'System32/wsl.exe'),'-d','Ubuntu-22.04','--','docker']
    command=[*docker,'run','--rm','--pull','never','--name',name,'--network','none','--read-only',
             '--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges',
             '--pids-limit','64','--memory','256m','--tmpfs','/tmp:rw,noexec,nosuid,size=32m']
    for source,target,readonly in [(ROOT/'runtime','/app/runtime',True),
            (ROOT/'scripts/mailbox_container_probe.py','/app/probe.py',True),
            (case/'fixture.json','/fixture.json',True),(ipc,'/ipc',False),(work,'/work',False)]:
        command.extend(['--mount',f'type=bind,source={wsl_path(source)},target={target}'+(',readonly' if readonly else '')])
    command.extend([image,'python','/app/probe.py'])
    completed=set();deadline=time.monotonic()+40
    with open(case/'docker.log','wb') as log:
        process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,creationflags=0x08000000)
        try:
            while process.poll() is None and time.monotonic()<deadline:
                for request in inbox.pending():
                    expected={'messages':[{'role':'user','content':'OFFLINE CONTAINER TRANSPORT FIXTURE'}]}
                    if request['body']!=expected: raise ValueError('Unexpected offline fixture request')
                    inbox.complete(request,body={'fixture':'received-by-trusted-offline-broker'})
                    completed.add(request['id'])
                time.sleep(.05)
            if process.poll() is None: raise TimeoutError('Container transport fixture timed out')
            if process.returncode: raise RuntimeError('Fixture failed; see local docker.log')
        finally:
            if process.poll() is None:
                subprocess.run([*docker,'stop','--time','1',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               timeout=10,creationflags=0x08000000)
                process.wait(timeout=10)
    result=json.loads((work/'result.json').read_text())
    result.update(requests=len(completed),image=image,container=name,
                  separateMailboxMount=True,productionEngineTested=False)
    if len(completed)!=1: raise ValueError('Fixture did not exchange exactly one request')
    atomic(case/'validation.json',result)
    print(json.dumps({'evidence':str(case/'validation.json'),'result':result},ensure_ascii=False))


if __name__=='__main__':main()
