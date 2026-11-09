"""Build an npm-compatible tgz with an explicit allowlist; no npm publishing."""
from pathlib import Path
import gzip
import hashlib
import io
import json
import tarfile
ROOT=Path(__file__).resolve().parents[1]
pkg=json.loads((ROOT/'package.json').read_text(encoding='utf-8'))
allowed=['package.json','index.js','lib','runtime','skills','scripts','tests','docs','cordis.patch.yml','README.md','LICENSE','THIRD_PARTY_NOTICES.md','config.example.json']
files=[]
for entry in allowed:
    path=ROOT/entry
    if not path.exists():raise SystemExit('Missing release input: '+entry)
    files.extend([path] if path.is_file() else [p for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'])
out=ROOT/'dist';out.mkdir(exist_ok=True);target=out/f"{pkg['name']}-{pkg['version']}.tgz"
with open(target,'wb') as raw,gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as gz,tarfile.open(fileobj=gz,mode='w') as tar:
    for path in sorted(set(files)):
        rel=path.relative_to(ROOT).as_posix();data=path.read_bytes()
        info=tarfile.TarInfo('package/'+rel);info.size=len(data);info.mtime=0;info.mode=0o644;tar.addfile(info,io.BytesIO(data))
sha=hashlib.sha256(target.read_bytes()).hexdigest()
(out/'SHA256SUMS.txt').write_text(sha+'  '+target.name+'\n',encoding='utf-8')
print(json.dumps({'package':str(target),'sha256':sha,'files':len(set(files))},ensure_ascii=False))
