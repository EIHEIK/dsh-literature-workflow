"""Invoke the exact registered login entry without logging the user out."""
import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time
import winreg

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,read_json,object_hash
from deployment_install import RUN_NAME,verify_installation
from service_main import local_call


def wait_stopped(path,previous_pid):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        state=read_json(path)
        if state.get('state')=='stopped' and state.get('pid')==previous_pid:return
        time.sleep(.1)
    raise TimeoutError('Existing service stop not observed; do not launch another')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    path=Path(args.config).resolve();config=read_json(path);installation=verify_installation(path)
    if config.get('paidWorkEnabled'):raise ValueError('Only unpaid installed validation is supported')
    state_path=path.parent/'service-state.json';prior=read_json(state_path)
    # An authenticated live response establishes ownership; an old state file
    # alone is insufficient to stop or assume that the process is still running.
    capabilities=local_call(path)['result']
    if capabilities['productionReady'] or capabilities['pilotReady']:raise ValueError('Unexpected paid readiness')
    local_call(path,stop=True);wait_stopped(state_path,prior['pid'])
    expected=[str(Path(installation['python']['executable']).with_name('pythonw.exe')),str(path.parent/'login_bootstrap.pyw')]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as registry:
        command,kind=winreg.QueryValueEx(registry,RUN_NAME)
    if command!=subprocess.list2cmdline(expected) or kind!=winreg.REG_SZ:raise ValueError('Login entry differs from the verified installation')
    started=time.time();launcher=subprocess.Popen(expected,cwd=path.parent,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=0x08000000)
    deadline=time.monotonic()+20;current=None
    while time.monotonic()<deadline:
        if launcher.poll() is not None:raise RuntimeError('Registered login launcher exited before service readiness')
        state=read_json(state_path)
        if state.get('state')=='running' and state.get('startedAt',0)>=started:
            current=state;break
        time.sleep(.1)
    if current is None:raise TimeoutError('Registered login launch remains unconfirmed; inspect this process before retrying')
    actual=local_call(path)['result']
    if actual['productionReady'] or actual['pilotReady'] or not actual['indexRefreshAvailable']:raise ValueError('Installed readiness changed')
    if current['url']!=prior['url']:raise ValueError('Installation restart changed endpoint')
    report={'passed':True,'installation':installation['id'],'release':installation['release'],
            'runtimeHash':current['runtimeHash'],'previousPid':prior['pid'],'currentPid':current['pid'],
            'registeredLoginCommandHash':object_hash(command),'exactLoginEntryExecuted':True,'stableEndpoint':True,
            'productionReady':False,'pilotReady':False,'modelCalls':0,'zoteroWrites':0,'leftRunning':True,
            'windowsLogoffOrRebootTested':False,'boundary':'Actual fixed installation and registered login command executed; no user logoff/reboot was performed'}
    atomic(args.output,report);print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
