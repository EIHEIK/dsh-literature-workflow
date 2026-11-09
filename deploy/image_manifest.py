"""Build-time inventory; no papers, credentials or controller code in this image."""
import hashlib
from importlib.metadata import distributions
import json
from pathlib import Path
import subprocess

base=Path('/opt/engine-home')
assets=[]
for path in sorted(base.rglob('*')):
    if path.is_file():
        assets.append({'path':path.relative_to(base).as_posix(),'bytes':path.stat().st_size,
                       'sha256':hashlib.file_digest(path.open('rb'),'sha256').hexdigest()})
result={'packages':sorted([{'name':d.metadata['Name'],'version':d.version} for d in distributions()],key=lambda d:d['name'].lower()),
        'assets':assets,'osPackages':subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\n'],text=True).splitlines()}
Path('/opt/engine-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'packages':len(result['packages']),'assets':len(assets),'assetBytes':sum(a['bytes'] for a in assets)}))
