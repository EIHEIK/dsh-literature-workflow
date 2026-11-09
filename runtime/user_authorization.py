"""Maintenance-only binding of an existing user instruction to frozen scope.

No model tool can mint confirmations. The adapter checks the actual local user
message and reconciled source inventory, and keeps an auditable private receipt.
"""
import json
from pathlib import Path

from core import atomic,object_hash,read_json
from model_gate import ModelGate

APPROVAL='我已经完成切换了，后续相关工作完成后，直接开始翻译就可以了'


def locate_instruction(sessions,thread):
    matches=list(Path(sessions).glob('**/rollout-*-'+thread+'.jsonl'))
    if len(matches)!=1:raise ValueError('Unique current conversation required')
    found=None
    with matches[0].open('rb') as stream:
        for raw in stream:
            try:value=json.loads(raw)
            except (ValueError,UnicodeError):continue
            payload=value.get('payload',{})
            if value.get('type')=='event_msg' and payload.get('type')=='user_message':
                message=payload.get('message')
                if isinstance(message,str) and message.strip()==APPROVAL:
                    found={'thread':thread,'timestamp':value.get('timestamp'),'messageSha256':object_hash(message),
                           'source':'local-host-user-message'}
            if value.get('type')=='response_item' and payload.get('role')=='user':
                message=''.join(item.get('text','') for item in payload.get('content',[]) if isinstance(item,dict))
                if message.strip()==APPROVAL:
                    found={'thread':thread,'timestamp':value.get('timestamp'),'messageSha256':object_hash(message),
                           'source':'local-host-user-message'}
    if found is None:raise ValueError('The actual user instruction was not found; assistant quotes cannot authorize a batch')
    return found


def bind_existing_authorization(store,batch_id,intake_path,sessions,thread):
    host=ModelGate(sessions,expected_effort=None).check(thread)
    instruction=locate_instruction(sessions,thread)
    inventory=read_json(intake_path);eligible={item['sourceSha256']:item for item in inventory['ready']}
    batch=store.batch(batch_id)
    if batch.get('runtimeHash')!=__import__('core').code_hash():raise ValueError('Batch runtime changed')
    for item in batch['items']:
        allowed=eligible.get(item['sourceSha256'])
        if allowed is None:raise ValueError('Source is outside the already authorized first batch')
        if (item.get('zotero') or {}).get('parentItemKey')!=allowed['zotero_key']:
            raise ValueError('Authorized parent changed')
        pages=item['pages']
        if pages!=list(range(1,allowed['pages']+1)) and not (allowed['pages']==20 and pages==[11,12,13]):
            raise ValueError('Page scope is outside the authorized full papers or existing three-page pilot')
    if batch['perPaperCap']!=10_000_000 or batch['perPaperTarget']!=5_000_000 or (batch['pilot'] and batch['pilotCap']!=15_000_000):
        raise ValueError('User instruction does not authorize changed budgets')
    body={'schema':1,'instruction':instruction,'batch':batch_id,'summaryHash':batch['summaryHash'],
          'items':batch['items'],'configHash':batch['configHash'],'runtimeHash':batch['runtimeHash'],
          'host':{k:host[k] for k in ('thread','turn','model','effort','source')},
          'scopeBasis':'reconciled-first-batch-and-existing-p11-13-pilot','budgetIncreaseAuthorized':False}
    receipt_hash=object_hash(body);destination=store.root/'user-authorizations'/(receipt_hash+'.json')
    atomic(destination,body)
    result=store.confirm(batch_id,batch['summaryHash'],'codex-user-instruction:'+receipt_hash)
    return {'batch':batch_id,'summaryHash':batch['summaryHash'],'confirmed':result['confirmed'],
            'receipt':str(destination),'receiptHash':receipt_hash,'modelCalls':0}
