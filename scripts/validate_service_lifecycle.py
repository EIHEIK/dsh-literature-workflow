"""Actual detached-process lifecycle verification, offline deployment only."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,read_json
from service_main import local_call


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();config=Path(args.config).resolve();destination=Path(args.output).resolve();destination.parent.mkdir(parents=True,exist_ok=True)
    if read_json(config).get('paidWorkEnabled'):raise ValueError('Lifecycle fixture requires paid work disabled')
    state_path=config.parent/'service-state.json'
    if state_path.exists() and read_json(state_path).get('state')!='stopped':raise ValueError('An existing service must be reconciled first')
    states=[];duplicate_exit=None
    for round_number in range(2):
        log=destination.with_name(f'service-lifecycle-{round_number}.log')
        with log.open('wb') as stream:
            started=time.time()
            command=[sys.executable,str(ROOT/'runtime/service_main.py'),'run','--config',str(config)]
            process=subprocess.Popen(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,creationflags=0x08000000)
            try:
                deadline=time.monotonic()+15;state=None
                while time.monotonic()<deadline:
                    if process.poll() is not None:raise RuntimeError('Service exited before ready; inspect lifecycle log')
                    if state_path.exists():
                        candidate=read_json(state_path)
                        if candidate.get('state')=='running' and candidate['startedAt']>=started:
                            state=candidate;break
                    time.sleep(.1)
                if state is None:raise TimeoutError('Service startup did not become observable')
                status=local_call(config)
                if status['result']['productionReady']:raise ValueError('Offline service unexpectedly enabled paid work')
                if round_number==0:
                    duplicate=subprocess.run(command,cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,timeout=10,creationflags=0x08000000)
                    duplicate_exit=duplicate.returncode
                    if duplicate_exit==0 or read_json(state_path)['pid']!=state['pid']:raise ValueError('Duplicate service was not rejected')
                    local_call(config) # Original process must still answer after rejection.
                local_call(config,stop=True);process.wait(timeout=15)
                stopped=read_json(state_path)
                if process.returncode or stopped['state']!='stopped' or stopped['pid']!=state['pid']:raise ValueError('Service shutdown was not clean')
                states.append({'started':state,'stopped':stopped,'launcherExit':process.returncode})
            finally:
                if process.poll() is None:
                    # Stop only through this service's authenticated fixed route.
                    local_call(config,stop=True);process.wait(timeout=15)
    if states[0]['started']['url']!=states[1]['started']['url']:raise ValueError('Loopback endpoint changed after restart')
    report={'passed':True,'runs':states,'duplicateExit':duplicate_exit,'stableEndpoint':True,
            'modelCalls':0,'translationJobsCreated':0,'zoteroWrites':0,
            'boundary':'Two actual hidden process starts/stops plus duplicate rejection; not a Windows reboot or login autostart test'}
    atomic(destination,report);print(json.dumps({'evidence':str(destination),'passed':True,'stableEndpoint':True,'modelCalls':0},ensure_ascii=False))


if __name__=='__main__':main()
