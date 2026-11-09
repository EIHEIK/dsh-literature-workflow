"""Read-only actual Work identity check. Does not create jobs or contact models."""
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from model_gate import ModelGate

thread=os.environ.get('CODEX_THREAD_ID')
root=Path.home()/'.codex/sessions'
result={'threadIdPresent':bool(thread),'threadId':thread,'sessionsRootExists':root.is_dir(),
        'modelRequests':0,'fileWrites':0,'mcpElicitation':'not_tested','pluginDiscovery':'not_tested'}
try:
    result['hostVerification']=ModelGate(root).check(thread)
    result['verified']=True
except Exception as error:
    result['verified']=False;result['errorType']=type(error).__name__
print(json.dumps(result,ensure_ascii=False))
