"""Convert pip's Linux resolution receipt into a hash-checked, wheel-only lock."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
report=json.loads((ROOT/'deploy/engine-resolution.json').read_text(encoding='utf-8'))
lines=['# Linux CPython 3.12 x86_64; resolved 2026-11-10; install with --require-hashes --only-binary=:all:.']
packages=[]
for item in report['install']:
    name=item['metadata']['name'];version=item['metadata']['version']
    url=item['download_info']['url'];digest=item['download_info']['archive_info']['hashes']['sha256']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+',version):
        raise ValueError('Unexpected distribution name/version')
    if urlsplit(url).scheme!='https' or urlsplit(url).hostname!='files.pythonhosted.org' or not urlsplit(url).path.endswith('.whl'):
        raise ValueError('Only official PyPI wheels are allowed')
    if not re.fullmatch(r'[0-9a-f]{64}',digest): raise ValueError('Missing wheel SHA256')
    packages.append((name.lower(),f'{name}=={version} --hash=sha256:{digest}'))
lines.extend(line for _,line in sorted(packages))
(ROOT/'deploy/engine-requirements.lock').write_text('\n'.join(lines)+'\n',encoding='utf-8')
# Keep the reproducibility receipt without megabytes of package descriptions.
compact={'version':report['version'],'pip_version':report.get('pip_version'),'environment':report.get('environment'),
         'install':[{'metadata':{k:i['metadata'][k] for k in ('name','version')},'download_info':i['download_info']} for i in report['install']]}
(ROOT/'deploy/engine-resolution.json').write_text(json.dumps(compact,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'packages':len(packages),'lock':'deploy/engine-requirements.lock'}))
