"""Short, fixed per-user login entry; configuration is adjacent, never a model argument."""
import json
from pathlib import Path
import runpy
import sys

private=Path(__file__).resolve().parent
try:
    config_path=private/'service.json'
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    launcher=Path(config['runtime']).parent/'scripts/service_launcher.py'
    sys.argv=[str(launcher),'--config',str(config_path)]
    runpy.run_path(str(launcher),run_name='__main__')
except Exception as error:
    (private/'login-error.json').write_text(json.dumps({'error':type(error).__name__,
        'message':'Login bootstrap could not start the verified service'}),encoding='utf-8')
    raise SystemExit(1)
