"""Silent Windows login launcher for a verified, fixed installation."""
import argparse
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic
from deployment_install import verify_installation


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=Path(args.config)
    try:
        receipt=verify_installation(config)
        if Path(receipt['release']).resolve()!=ROOT:raise ValueError('Launcher does not match the active release')
        from service_main import run
        run(config)
    except Exception as error:
        atomic(config.parent/'launcher-error.json',{'at':time.time(),'error':type(error).__name__,
                'message':'Fixed service launch failed; inspect installation and current service before retrying'})
        return 1
    return 0


if __name__=='__main__':sys.exit(main())
