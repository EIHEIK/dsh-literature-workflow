"""Trusted Windows installation/rollback utilities, never exposed as model tools.

Versioned code snapshots are immutable by convention and checked at every launch.
Rollback switches code only; it never restores an older database over costs or
Zotero receipts. Configuration and SQLite backups stay in the private directory.
"""
import json
import os
from pathlib import Path
import shutil
import secrets
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

from core import atomic,digest,object_hash,read_json
from instance_lock import InstanceLock

ENTRIES=('package.json','index.js','lib','runtime','skills','scripts','tests','docs','deploy','plugins',
         'cordis.patch.yml','README.md','LICENSE','THIRD_PARTY_NOTICES.md','config.example.json')
RUN_NAME='FlyLiteratureWorkflowV2'
SCHEMA=2


def plain_path(path):
    path=Path(path).absolute()
    for part in (path,*path.parents):
        if part.exists() and (part.is_symlink() or getattr(part.lstat(),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Deployment paths cannot contain links or reparse points')
    return path.resolve()


def snapshot(source,releases):
    source=plain_path(source);releases=plain_path(releases);releases.mkdir(parents=True,exist_ok=True)
    files={}
    for name in ENTRIES:
        path=source/name
        if not path.exists():raise ValueError('Missing installation source: '+name)
        candidates=[path] if path.is_file() else path.rglob('*')
        for candidate in candidates:
            if '__pycache__' in candidate.parts or candidate.suffix=='.pyc':continue
            plain_path(candidate)
            if candidate.is_file():files[candidate.relative_to(source).as_posix()]=digest(candidate)
    body={'schema':SCHEMA,'files':files};release_id='release-'+object_hash(body)[:24]
    destination=releases/release_id
    if destination.exists():verify_release(destination,object_hash(body));return destination,object_hash(body)
    staging=releases/('.staging-'+uuid.uuid4().hex);staging.mkdir()
    for name,sha in files.items():
        target=staging/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,target)
        if digest(target)!=sha:raise ValueError('Installation source changed during copy')
    atomic(staging/'release-manifest.json',body)
    # Same parent and filesystem; a partial copy never becomes current.
    staging.rename(destination);verify_release(destination,object_hash(body))
    return destination,object_hash(body)


def verify_release(release,expected_hash):
    release=plain_path(release);manifest=read_json(release/'release-manifest.json')
    if object_hash(manifest)!=expected_hash or manifest.get('schema')!=SCHEMA:raise ValueError('Release manifest changed or schema unsupported')
    files=manifest.get('files')
    if not isinstance(files,dict) or not files:raise ValueError('Empty release')
    for name,sha in files.items():
        path=plain_path(release/name)
        if not path.is_relative_to(release) or digest(path)!=sha:raise ValueError('Installed release content changed')
    # Additional executable files could be imported despite unchanged originals.
    actual={p.relative_to(release).as_posix() for p in release.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and p.name!='release-manifest.json'}
    if actual!=set(files):raise ValueError('Unexpected files in installed release')
    return manifest


def python_fingerprint():
    import pymupdf
    executable=Path(sys.executable).with_name('python.exe') if os.name=='nt' else Path(sys.executable)
    base=Path(getattr(sys,'_base_executable',sys.executable))
    if os.name=='nt':base=base.with_name('python.exe')
    binaries={str(executable):digest(executable),str(base):digest(base)}
    if os.name=='nt':
        for candidate in (executable.with_name('pythonw.exe'),base.with_name('pythonw.exe')):
            binaries[str(candidate)]=digest(candidate)
    module=Path(pymupdf.__file__).parent
    files={p.relative_to(module).as_posix():digest(p) for p in module.rglob('*') if p.is_file() and p.suffix in ('.py','.pyd','.dll')}
    return {'executable':str(executable),'version':sys.version,'binaries':binaries,'pymupdfVersion':pymupdf.VersionBind,
            'pymupdfRoot':str(module),'pymupdfFiles':files}


def verify_installation(config_path,*,dependencies=True):
    config_path=plain_path(config_path);config=read_json(config_path)
    receipt=read_json(config['deploymentReceipt']);release=Path(receipt['release'])
    if config.get('deploymentId')!=receipt['id'] or Path(config['runtime']).resolve()!=release/'runtime':
        raise ValueError('Installed configuration identity changed')
    verify_release(release,receipt['manifestHash'])
    if dependencies and python_fingerprint()!=receipt['python']:raise ValueError('Installed Python dependency fingerprint changed')
    return receipt


def backup_private(config_path,config):
    backup=config_path.parent/'backups'/('upgrade-'+str(time.time_ns()));backup.mkdir(parents=True)
    shutil.copyfile(config_path,backup/'service.json')
    state=Path(config['data'])/'state.sqlite3'
    if state.exists():
        original=sqlite3.connect(str(state));copy=sqlite3.connect(str(backup/'state.sqlite3'))
        try:original.backup(copy)
        finally:copy.close();original.close()
    return backup


def activate(config_path,release,manifest_hash,*,kind='install'):
    config_path=plain_path(config_path);release=plain_path(release);verify_release(release,manifest_hash)
    python=python_fingerprint();config=read_json(config_path);data=Path(config['data'])
    with InstanceLock(data),InstanceLock(data/'library-scan/manager-lock'):
        backup=backup_private(config_path,config)
        record={'id':'installation-'+uuid.uuid4().hex,'schema':SCHEMA,'kind':kind,'release':str(release),
                'manifestHash':manifest_hash,'python':python,'createdAt':time.time(),'backup':str(backup),
                'previousRuntime':config['runtime'],'dataPreserved':str(data)}
        receipt=config_path.parent/'installations'/(record['id']+'.json');atomic(receipt,record)
        config.update(runtime=str(release/'runtime'),imageLock=str(release/'deploy/engine-image.lock.json'),
                      priceBook=str(release/'deploy/deepseek-pricing.json'),paidWorkEnabled=False,
                      deploymentId=record['id'],deploymentReceipt=str(receipt),
                      pilotReadinessReceipt=config.get('pilotReadinessReceipt',str(config_path.parent/'pilot-readiness.json')))
        config.setdefault('supervisionMode','codex')
        config['credentials'].setdefault('codex',secrets.token_urlsafe(40))
        atomic(config_path,config)
    autostart=config_path.parent/'autostart.json'
    if autostart.exists() and read_json(autostart).get('enabled') is True:
        configure_autostart(config_path,True)
    return record


def install(source,releases,config_path):
    release,manifest_hash=snapshot(source,releases)
    return activate(config_path,release,manifest_hash)


def rollback(config_path,installation_id):
    if not isinstance(installation_id,str) or not __import__('re').fullmatch('installation-[a-f0-9]{32}',installation_id):
        raise ValueError('A recorded installation ID is required')
    previous=read_json(Path(config_path).parent/'installations'/(installation_id+'.json'))
    if previous['schema']!=SCHEMA:raise ValueError('Database compatibility is not verified for this rollback')
    return activate(config_path,previous['release'],previous['manifestHash'],kind='rollback-code-only')


def launch_command(config_path):
    receipt=verify_installation(config_path)
    executable=Path(receipt['python']['executable']).with_name('pythonw.exe')
    return [str(executable),str(Path(receipt['release'])/'scripts/service_launcher.py'),'--config',str(Path(config_path).resolve())]


def start_hidden(config_path):
    command=launch_command(config_path)
    child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           creationflags=0x08000000,cwd=str(Path(command[1]).parent))
    return {'launcherPid':child.pid,'started':True,'readyVerified':False}


def configure_autostart(config_path,enabled):
    if os.name!='nt':raise ValueError('This deployment supports Windows user login only')
    import winreg
    config_path=plain_path(config_path);installation=verify_installation(config_path)
    bootstrap=config_path.parent/'login_bootstrap.pyw'
    source=Path(installation['release'])/'scripts/login_bootstrap.pyw'
    receipt_path=config_path.parent/'autostart.json'
    prior=read_json(receipt_path) if receipt_path.exists() else {}
    if bootstrap.exists() and digest(bootstrap) not in (digest(source),prior.get('bootstrapSha256')):
        raise ValueError('Existing login bootstrap has unrecognized changes')
    executable=Path(installation['python']['executable']).with_name('pythonw.exe')
    command=subprocess.list2cmdline([str(executable),str(bootstrap)])
    if len(command.encode('utf-16-le'))//2>260:
        raise ValueError('Windows Run command exceeds 260 characters; choose a shorter installation directory')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run',0,winreg.KEY_READ|winreg.KEY_WRITE) as key:
        try:previous,kind=winreg.QueryValueEx(key,RUN_NAME)
        except FileNotFoundError:previous=None
        owned=prior.get('command')
        if previous is not None and previous not in (command,owned):raise ValueError('Existing login entry is not owned by this installation')
        if enabled:
            shutil.copyfile(source,bootstrap)
            if digest(bootstrap)!=digest(source):raise ValueError('Login bootstrap copy changed')
            winreg.SetValueEx(key,RUN_NAME,0,winreg.REG_SZ,command)
        elif previous is not None:winreg.DeleteValue(key,RUN_NAME)
        try:current,_=winreg.QueryValueEx(key,RUN_NAME)
        except FileNotFoundError:current=None
    if current!=(command if enabled else None):raise ValueError('Login entry verification failed')
    atomic(receipt_path,{'enabled':enabled,'name':RUN_NAME,'command':command,'at':time.time(),'requiresAdmin':False,
                        'bootstrapSha256':digest(source)})
    return {'enabled':enabled,'name':RUN_NAME,'requiresAdmin':False,'windowsLoginTested':False}
