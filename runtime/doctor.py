import importlib.metadata
import json
import os
import sys
from worker import probe

def doctor(config):
    checks={'python':sys.version.split()[0],'apiKeyPresent':bool(os.environ.get('DEEPSEEK_API_KEY'))}
    try:
        import pymupdf as fitz
        checks['pymupdf']=fitz.VersionBind
    except ImportError:checks['pymupdf']=None
    try:checks['engine']=probe(config.get('engine',''));checks['engine']['help']='[capability checks passed]'
    except Exception as e:checks['engineError']=str(e)
    checks['readyForTranslation']=bool(checks['apiKeyPresent'] and checks['pymupdf'] and checks.get('engine'))
    checks['note']='No model request, installation or Zotero write was performed. Real host/API/PDF quality acceptance remains separate.'
    return checks

if __name__=='__main__':
    try:print(json.dumps({'ok':True,'result':doctor(json.load(sys.stdin))},ensure_ascii=False))
    except Exception as e:print(json.dumps({'ok':False,'error':str(e)}));sys.exit(1)
