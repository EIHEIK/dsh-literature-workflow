"""Fixed, reattachable network-none Docker worker runner (Windows + WSL).

Handlers are installed trusted code. Their prepare/finish methods convert frozen
job inputs and verify stage-specific evidence. No caller can supply shell text,
image names, mount paths, container options, credentials or completion verdicts.
"""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time

from api_mailbox import MailboxInbox,read_bounded
from broker_pump import MailboxScope
from core import atomic,digest,object_hash,read_json


def linux_path(path):
    path=Path(path).resolve(strict=True)
    if not re.fullmatch('[A-Za-z]:',path.drive) or ',' in str(path):raise ValueError('Only local Windows drive paths without commas are supported')
    return '/mnt/'+path.drive[0].lower()+'/'+path.as_posix()[3:]


class DockerRunner:
    def __init__(self,root,runtime,image_lock,handlers,*,distro='Ubuntu-22.04'):
        self.root=Path(root).resolve();self.runtime=Path(runtime).resolve(strict=True)
        self.image=read_json(image_lock)['imageId'];self.handlers=handlers;self.store=None
        if not re.fullmatch('sha256:[a-f0-9]{64}',self.image):raise ValueError('Validated immutable image ID required')
        if distro!='Ubuntu-22.04':raise ValueError('Unvalidated Docker host')
        self.command=[str(Path(os.environ['WINDIR'])/'System32/wsl.exe'),'-d',distro,'--','docker']
        self.control=self.root/'worker-control';self.control.mkdir(parents=True,exist_ok=True)
        self.mailboxes=self.root/'mailboxes';self.mailboxes.mkdir(parents=True,exist_ok=True)

    def bind(self,store):self.store=store

    def storage_ready(self):
        return shutil.disk_usage(Path(os.environ['SystemDrive']+'\\')).free>=4*1024**3

    def docker(self,args,timeout=15):
        return subprocess.run([*self.command,*args],capture_output=True,encoding='utf-8',errors='replace',
                              timeout=timeout,creationflags=0x08000000)

    def identity(self,phase):
        if not re.fullmatch('paper-[a-f0-9]{16}',phase['job']) or phase['stage'] not in self.handlers or type(phase['generation']) is not int:
            raise ValueError('Unregistered worker stage or identity')
        return 'lit-'+phase['job']+'-'+phase['stage']+'-'+str(phase['generation'])

    def record_path(self,phase):
        # Cancellation invalidates the generation; retain the recorded worker.
        name=phase.get('worker') or self.identity(phase)
        if not re.fullmatch(r'lit-paper-[a-f0-9]{16}-[a-z]+-\d+',name):raise ValueError('Invalid worker name')
        return self.control/(name+'.json')

    def start(self,phase,job):
        if self.store is None:raise RuntimeError('Runner is not bound to its controller')
        name=self.identity(phase);record_path=self.record_path(phase)
        if record_path.exists():raise ValueError('Existing worker intent must be inspected before recovery')
        if not self.storage_ready():
            raise OSError('Docker host volume has insufficient space')
        prepared=self.handlers[phase['stage']].prepare(self.store,phase,job)
        work=self.store.folder(job)/'stages'/name;work.mkdir(parents=True)
        ipc=self.mailboxes/name;ipc.mkdir()
        config=self.control/(name+'.worker.json');credential=secrets.token_urlsafe(32)
        worker={'schema':1,'job':job['id'],'generation':phase['generation'],'stage':phase['stage'],
                'sourceSha256':job['sourceSha256'],'sourcePages':job['sourcePages'],
                'credential':credential,'settings':prepared['settings']}
        atomic(config,worker)
        mounts=[(self.runtime,'/app/runtime',True),(Path(job['source']),'/input/source.pdf',True),
                (config,'/input/stage.json',True),(ipc,'/ipc',False),(work,'/work',False)]
        for target,path in prepared.get('inputs',{}).items():
            if target!='/input/candidate' and not re.fullmatch(r'/input/[a-z0-9_-]+\.(json|pdf|txt|csv)',target):raise ValueError('Invalid prepared input mount')
            if target in {m[1] for m in mounts}:raise ValueError('Duplicate input mount')
            mounts.append((Path(path).resolve(strict=True),target,True))
        normalized=[{'source':linux_path(p),'target':target,'readonly':readonly} for p,target,readonly in mounts]
        record={'worker':name,'job':job['id'],'generation':phase['generation'],'stage':phase['stage'],
                'sourceSha256':job['sourceSha256'],'token':phase['token'],'credential':credential,
                'work':str(work),'ipc':str(ipc),'config':str(config),'configHash':digest(config),
                'policy':prepared.get('policy'),'mounts':normalized,'image':self.image}
        # Private immutable intent precedes launch; a lost spawn ACK never causes
        # an immediate duplicate. Next tick inspects this deterministic name.
        atomic(record_path,record)
        args=['run','--detach','--pull','never','--name',name,'--label','literature.job='+job['id'],
              '--label','literature.config='+record['configHash'],'--network','none','--read-only',
              '--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges',
              '--pids-limit','256','--memory','3g','--cpus','2','--tmpfs','/tmp:rw,noexec,nosuid,size=256m',
              '-e','HOME=/tmp/literature-home','-e','XDG_CACHE_HOME=/tmp/literature-home/.cache']
        for mount in normalized:
            args.extend(['--mount',f'type=bind,source={mount["source"]},target={mount["target"]}'+(',readonly' if mount['readonly'] else '')])
        args.extend([self.image,'python','/app/runtime/container_worker.py'])
        result=self.docker(args,timeout=30)
        if result.returncode:raise RuntimeError('Docker worker launch did not acknowledge success')

    def _observe(self,phase):
        path=self.record_path(phase)
        if not path.exists():return None,{'state':'missing','worker':phase.get('worker'),'checkedAt':time.time()}
        record=read_json(path)
        try:result=self.docker(['container','inspect',record['worker']],timeout=8)
        except (OSError,subprocess.TimeoutExpired):return record,{'state':'unavailable'}
        if result.returncode:
            # Confirm daemon responsiveness before treating not-found as absence.
            try:healthy=self.docker(['info','--format','{{.ServerVersion}}'],timeout=8)
            except (OSError,subprocess.TimeoutExpired):return record,{'state':'unavailable'}
            if healthy.returncode or 'No such container' not in result.stderr:return record,{'state':'unavailable'}
            return record,{'state':'missing','worker':record['worker'],'checkedAt':time.time()}
        data=json.loads(result.stdout)[0];host=data['HostConfig'];labels=data['Config'].get('Labels') or {}
        actual=sorted((m['Source'],m['Destination'],not m['RW']) for m in data['Mounts'] if m['Type']=='bind')
        expected=sorted((m['source'],m['target'],m['readonly']) for m in record['mounts'])
        if (data['Image']!=record['image'] or host['NetworkMode']!='none' or not host['ReadonlyRootfs'] or
            data['Config']['User']!='10001:10001' or 'ALL' not in (host.get('CapDrop') or []) or
            not any(v.startswith('no-new-privileges') for v in host.get('SecurityOpt',[])) or
            host.get('Privileged') or host.get('PidMode')=='host' or host.get('CapAdd') or actual!=expected or
            host.get('Memory')!=3*1024**3 or host.get('PidsLimit')!=256 or host.get('NanoCpus')!=2_000_000_000 or
            any(m['Type']!='bind' and not(m['Type']=='tmpfs' and m['Destination']=='/tmp') for m in data['Mounts']) or
            labels.get('literature.config')!=record['configHash'] or labels.get('literature.job')!=record['job'] or
            digest(record['config'])!=record['configHash']):
            raise ValueError('Worker isolation or frozen intent mismatch')
        state=data['State'];observed={'state':'running' if state['Running'] else 'terminal',
                'worker':record['worker'],'checkedAt':time.time(),'exitCode':state.get('ExitCode')}
        return record,observed

    def inspect(self,phase):
        record,observed=self._observe(phase)
        if observed['state']=='running':
            progress=Path(record['work'])/'progress.json'
            if progress.exists():observed['progressFingerprint']=object_hash(read_bounded(progress,Path(record['work']),65536))
            return observed
        if observed['state']!='terminal' or observed['exitCode']!=0:return observed
        try:
            result=read_bounded(Path(record['work'])/'result.json',Path(record['work']),1024*1024)
            if any(result.get(k)!=record[k] for k in ('job','generation','stage','sourceSha256')):
                raise ValueError('Worker result identity mismatch')
            # A worker-written result cannot accept its own translation. The trusted
            # stage handler verifies program checks and private provider receipts.
            verified=self.handlers[phase['stage']].finish(self.store,phase,record,result)
        except (ValueError,OSError,KeyError,TypeError):
            return {**observed,'state':'failed','kind':'evidence','message':'Worker output failed private receipt or artifact validation'}
        return {**observed,**verified,'state':'completed'}

    def cancel(self,phase):
        record,observed=self._observe(phase)
        if observed['state']!='running':return observed
        try:self.docker(['stop','--time','2',record['worker']],timeout=8)
        except (OSError,subprocess.TimeoutExpired):return {'state':'unavailable'}
        return self._observe(phase)[1]

    def mailbox_scopes(self,store):
        scopes=[]
        for body, in store.db.execute('SELECT body FROM phase_jobs'):
            phase=json.loads(body)
            if phase['stage'] not in self.handlers:continue
            if phase['state']!='running' or not phase.get('worker'):continue
            path=self.record_path(phase)
            if not path.exists():continue
            record=read_json(path)
            if not record.get('policy'):continue
            if phase['token']!=record['token'] or phase['generation']!=record['generation']:raise ValueError('Mailbox lease mismatch')
            scopes.append(MailboxScope(phase['job'],phase['token'],phase['generation'],
                MailboxInbox(record['ipc'],record['credential'],record['generation']),record['policy']))
        return scopes
