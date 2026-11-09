"""Real offline image validation. No provider credential, request or paper."""
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import subprocess

checks={'nonRoot':os.geteuid()==10001}
try:
    Path('/etc/literature-probe').write_text('denied');checks['rootReadOnly']=False
except OSError:checks['rootReadOnly']=True
try:
    socket.create_connection(('1.1.1.1',443),timeout=2).close();checks['networkDenied']=False
except OSError:checks['networkDenied']=True
checks['noDockerSocket']=not Path('/var/run/docker.sock').exists()
status=Path('/proc/self/status').read_text()
checks['capabilitiesDropped']='CapEff:\t0000000000000000' in status
checks['noNewPrivileges']='NoNewPrivs:\t1' in status
manifest=json.loads(Path('/opt/engine-manifest.json').read_text())
for item in manifest['assets']:
    with open(Path('/opt/engine-home')/item['path'],'rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=item['sha256']:raise ValueError('Changed image asset')
checks['assetHashes']=True
cache=Path.home()/'.cache/babeldoc';cache.mkdir(parents=True,exist_ok=True)
for folder in ('fonts','models','cmap','tiktoken'):
    (cache/folder).symlink_to(Path('/opt/engine-home/.cache/babeldoc')/folder,target_is_directory=True)
result=subprocess.run(['babeldoc','--warmup'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90)
Path('/work/offline-warmup.log').write_bytes(result.stdout)
checks['offlineWarmup']=result.returncode==0
from babeldoc.docvision.doclayout import OnnxModel
model=OnnxModel.from_pretrained()
checks['layoutModelLoaded']=model is not None
import pymupdf as fitz
doc=fitz.open();page=doc.new_page();page.insert_text((50,60),'中文字体与可搜索文字测试',fontname='china-s',fontsize=14)
doc.save('/work/font-fixture.pdf');doc.close()
with fitz.open('/work/font-fixture.pdf') as doc:
    checks['chineseTextSearchable']='中文字体' in doc[0].get_text()
    pix=doc[0].get_pixmap(matrix=fitz.Matrix(1,1));pix.save('/work/font-fixture.png')
checks['pinnedVersions']=version('pdf2zh-next')=='2.10.2' and version('babeldoc')=='0.7.1'
report={'passed':all(checks.values()),'checks':checks,'assetCount':len(manifest['assets']),
        'versions':{p:version(p) for p in ('pdf2zh-next','babeldoc','pymupdf','onnxruntime')},
        'modelCalls':0,'boundary':'Image isolation, assets, layout initialization, text and rendering only; translation quality untested'}
Path('/work/validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))
raise SystemExit(0 if report['passed'] else 1)
