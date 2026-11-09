"""Maintenance command; prepare and bind existing authorization, never start/spend."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from batches import BatchStore
from core import read_json
from instance_lock import InstanceLock
from legacy_import import prepare_pilot

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--legacy',required=True);p.add_argument('--intake',required=True);a=p.parse_args()
    config=read_json(a.config)
    if config.get('paidWorkEnabled'):raise ValueError('Prepare migration before enabling paid work')
    with InstanceLock(config['data']):
        store=BatchStore(config['data'])
        try:result=prepare_pilot(store,a.legacy,a.intake,config['sessionsRoot'],os.environ.get('CODEX_THREAD_ID'))
        finally:store.db.close()
    print(json.dumps(result,ensure_ascii=False))
