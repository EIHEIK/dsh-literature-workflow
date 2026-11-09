"""Explicit maintenance CLI; not a Dot/DSH tool."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from deployment_install import install,verify_installation,configure_autostart,start_hidden,rollback


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['install','verify','start','enable-login','disable-login','rollback'])
    p.add_argument('--config',required=True);p.add_argument('--releases');p.add_argument('--installation');args=p.parse_args()
    if args.action=='install':
        if not args.releases:p.error('--releases is required')
        value=install(ROOT,args.releases,args.config)
    elif args.action=='verify':value=verify_installation(args.config)
    elif args.action=='start':value=start_hidden(args.config)
    elif args.action=='rollback':value=rollback(args.config,args.installation)
    else:value=configure_autostart(args.config,args.action=='enable-login')
    # No private configuration, credentials or dependency inventory in stdout.
    print(json.dumps({k:value[k] for k in ('id','release','backup','kind','dataPreserved','launcherPid','started','readyVerified','enabled','name','requiresAdmin','windowsLoginTested') if k in value},ensure_ascii=False))
