"""Controller-owned request ledger, reservations, private cache and API policy.

No network I/O occurs in this module. Call prepare/finish on the single-writer
controller thread; only the returned bounded provider payload crosses to an HTTP
executor. Shared worker response files are never treated as billing evidence.
"""
import base64
from datetime import date
import json
from pathlib import Path
import time
import uuid

from budget import BudgetExceeded
from core import atomic, canonical, digest, object_hash, read_json


class BrokerWait(ValueError): pass
class BrokerBlocked(ValueError): pass


def integer(value, maximum=10**9):
    if type(value) is not int or not 0<=value<=maximum: raise ValueError('Invalid provider usage count')
    return value


class PriceBook:
    def __init__(self, data):
        if data.get('model')!='deepseek-flash' or data.get('currency')!='CNY':
            raise ValueError('Only the verified DeepSeek Flash CNY tariff is configured')
        self.data=data
        self.rates=data['peakMicrosPerMillion']
        if set(self.rates)!={'inputMiss','inputHit','output'}: raise ValueError('Incomplete tariff')
        for value in self.rates.values(): integer(value)
        if not 0<self.rates['inputHit']<=self.rates['inputMiss']<=self.rates['output']:
            raise ValueError('Invalid tariff order')
        if integer(data['contextTokenCeiling'])<1048576: raise ValueError('Reservation cannot use an unverified smaller context ceiling')
        if data.get('accounting')!='usage_at_peak_rate_upper_bound': raise ValueError('Accounting convention must be explicit')
        rules=data.get('inputReservation')
        if rules is not None and (rules.get('method')!='verified_byte_bpe_and_images_v1' or
            rules.get('textByteMultiplier')!=2 or rules.get('perMessageTokens')!=1024 or
            rules.get('fixedTokens')!=4096 or rules.get('maxImageTokens')!=1024 or
            rules.get('tokenizerSha256')!='c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b'):
            raise ValueError('Unverified prompt reservation rules')
        self.hash=object_hash(data)

    def check_current(self):
        today=date.today()
        if not date.fromisoformat(self.data['verifiedDate'])<=today<=date.fromisoformat(self.data['validThrough']):
            raise BrokerBlocked('Verified tariff expired or is not yet valid')

    def cost(self, hit, miss, output):
        value=integer(hit)*self.rates['inputHit']+integer(miss)*self.rates['inputMiss']+integer(output)*self.rates['output']
        return (value+999999)//1000000

    def input_bound(self,payload=None):
        ceiling=self.data['contextTokenCeiling'];rules=self.data.get('inputReservation')
        if rules is None or payload is None:return ceiling
        text_bytes=0;images=0
        for message in payload['messages']:
            content=message['content']
            if isinstance(content,str):text_bytes+=len(content.encode('utf-8'))
            else:
                for part in content:
                    if part['type']=='text':text_bytes+=len(part['text'].encode('utf-8'))
                    elif part['type']=='image_url':images+=1
                    else:raise ValueError('Unbounded prompt part')
        # Verified tokenizer: byte-level BPE, empty normalization sequence, no
        # prefix-space insertion. Merging cannot create more text tokens than
        # UTF-8 bytes. Double that count, plus generous framing allowances;
        # inline image contents use the independently documented 1024-token cap.
        return min(ceiling,text_bytes*rules['textByteMultiplier']+
                   len(payload['messages'])*rules['perMessageTokens']+rules['fixedTokens']+
                   images*rules['maxImageTokens'])

    def reservation(self, max_output,payload=None):
        self.check_current()
        return self.cost(0,self.input_bound(payload),max_output)

    def settlement(self, usage):
        if not isinstance(usage,dict): raise ValueError('Provider usage is missing')
        prompt=integer(usage.get('prompt_tokens')); output=integer(usage.get('completion_tokens'))
        total=integer(usage.get('total_tokens'))
        if total!=prompt+output: raise ValueError('Inconsistent total token usage')
        details=usage.get('prompt_tokens_details') or {}
        if not isinstance(details,dict): raise ValueError('Invalid usage details')
        hit=usage.get('prompt_cache_hit_tokens',details.get('cached_tokens',0))
        hit=integer(hit); miss=integer(usage.get('prompt_cache_miss_tokens',prompt-hit))
        if hit+miss!=prompt: raise ValueError('Inconsistent prompt token usage')
        # Completion tokens already include reasoning; do not count it again.
        return self.cost(hit,miss,output)


def provider_payload(body, policy):
    if not isinstance(body,dict) or set(body)-{'messages','model','stream','max_tokens','temperature','top_p','response_format','thinking','reasoning_effort'}:
        raise BrokerBlocked('Unsupported model request fields; tools and remote URLs are not enabled')
    messages=body.get('messages')
    if not isinstance(messages,list) or not 1<=len(messages)<=100: raise BrokerBlocked('Invalid model message count')
    checked=[]; image_count=0
    for message in messages:
        if not isinstance(message,dict) or set(message)!={'role','content'} or message['role'] not in ('system','user','assistant'):
            raise BrokerBlocked('Unsupported message role or fields')
        content=message['content']
        if isinstance(content,str):
            if not content.strip(): raise BrokerBlocked('Empty message content')
        elif isinstance(content,list) and message['role']=='user' and 1<=len(content)<=20:
            for part in content:
                if not isinstance(part,dict): raise BrokerBlocked('Invalid content part')
                if part.get('type')=='text':
                    if set(part)!={'type','text'} or not isinstance(part['text'],str) or not part['text'].strip():
                        raise BrokerBlocked('Invalid text part')
                elif part.get('type')=='image_url':
                    if set(part)!={'type','image_url'} or not isinstance(part['image_url'],dict): raise BrokerBlocked('Invalid image part')
                    image=part['image_url']
                    if set(image)-{'url','detail'} or image.get('detail','original') not in ('low','high','original','auto'):
                        raise BrokerBlocked('Invalid image options')
                    url=image.get('url','')
                    if not isinstance(url,str) or not url.startswith(('data:image/png;base64,','data:image/jpeg;base64,','data:image/webp;base64,','data:image/gif;base64,')):
                        raise BrokerBlocked('Only inline images are allowed; no remote URL or Files API')
                    try: decoded=base64.b64decode(url.split(',',1)[1],validate=True)
                    except ValueError: raise BrokerBlocked('Invalid inline image encoding') from None
                    if not decoded.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'GIF87a',b'GIF89a',b'RIFF')):
                        raise BrokerBlocked('Unrecognized inline image bytes')
                    image_count+=1
                else: raise BrokerBlocked('Unsupported content part')
        else: raise BrokerBlocked('Invalid message content')
        checked.append(message)
    if image_count>8: raise BrokerBlocked('Split visual review into bounded page groups')
    # Configuration is controller-owned and frozen in the batch fingerprint.
    maximum=policy['maxOutputTokens']
    if type(maximum) is not int or not 1<=maximum<=32768: raise ValueError('Invalid fixed output limit')
    if policy['model']!='deepseek-flash' or policy['thinking'] not in ('enabled','disabled'):
        raise ValueError('Unsupported fixed provider policy')
    payload={'model':policy['model'],'messages':checked,'stream':False,'max_tokens':maximum,
             'thinking':{'type':policy['thinking']}}
    if policy['thinking']=='enabled': payload['reasoning_effort']='high'
    if policy.get('jsonOutput'): payload['response_format']={'type':'json_object'}
    elif policy.get('allowJsonRequest') and body.get('response_format') is not None:
        if body['response_format']!={'type':'json_object'}:raise BrokerBlocked('Unsupported response format')
        payload['response_format']={'type':'json_object'}
    if len(canonical(payload).encode('utf-8'))>8*1024*1024: raise BrokerBlocked('Provider request too large')
    return payload


class ModelBroker:
    def __init__(self, store, pricebook):
        self.store=store; self.book=pricebook
        if store.db.in_transaction: raise ValueError('Initialize broker outside a transaction')
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,attempt INTEGER NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS model_call_cache ON model_calls(job_id,fingerprint,attempt);
            CREATE TABLE IF NOT EXISTS broker_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_deliveries(job_id TEXT NOT NULL,stage TEXT NOT NULL,
                generation INTEGER NOT NULL,request_id TEXT NOT NULL,call_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,PRIMARY KEY(job_id,generation,request_id));
        ''')
        self.private=store.root/'broker-private';self.private.mkdir(exist_ok=True)

    def save(self, call):
        self.store.db.execute('UPDATE model_calls SET state=?,body=? WHERE id=?',
                              (call['state'],canonical(call),call['id']))

    def get(self, call_id):
        row=self.store.db.execute('SELECT body FROM model_calls WHERE id=?',(call_id,)).fetchone()
        if row is None: raise ValueError('Unknown model call')
        return json.loads(row[0])

    def _active(self, job_id, phase_token):
        phase=self.store.valid_lease(job_id,phase_token);batch=self.store.batch(phase['batch'])
        if batch['state']!='running' or not batch['confirmation']: raise BrokerWait('Batch does not permit new model requests')
        if phase['stage'] not in ('background','translate','review','repair'):
            raise BrokerBlocked('This stage has no model request capability')
        self.store.verify_checkpoints(phase)
        return phase,batch,self.store.get(job_id)

    def prepare(self, job_id, phase_token, body, policy):
        """Commit reservation + dispatch intent BEFORE the executor sends bytes."""
        payload=provider_payload(body,policy)
        with self.store.transaction():
            phase,batch,job=self._active(job_id,phase_token)
            fingerprint=object_hash({'payload':payload,'policy':policy,'source':job['sourceSha256'],
                'config':batch['configHash'],'runtime':batch['runtimeHash'],'stage':phase['stage'],
                'dependency':phase.get('lastCheckpoint'),'candidate':(job.get('candidate') or {}).get('id')})
            rows=self.store.db.execute('SELECT body FROM model_calls WHERE job_id=? AND fingerprint=? ORDER BY attempt DESC',
                                       (job_id,fingerprint)).fetchall()
            previous=json.loads(rows[0][0]) if rows else None
            if previous and previous['state']=='completed':
                path=Path(previous['response']['path']).resolve(strict=True)
                if not path.is_relative_to(self.private.resolve()) or digest(path)!=previous['response']['sha256']:
                    raise BrokerBlocked('Trusted response cache changed')
                response=read_json(path)
                return {'action':'cached','call':previous['id'],'response':response}
            if previous and previous['state']=='dispatched': raise BrokerWait('Matching provider request is already in flight')
            if previous and (previous['state']=='rejected' or not previous.get('retryable')):
                raise BrokerBlocked('Previous result requires explicit repair or issue handling')
            if previous and previous['attempt']>=4: raise BrokerBlocked('Network retry limit reached')
            if previous and previous.get('retryAt',0)>time.time(): raise BrokerWait('Provider retry delay is active')
            fault=self.store.db.execute("SELECT value FROM broker_meta WHERE key='fault'").fetchone()
            if fault: raise BrokerBlocked('Shared provider fault requires reconciliation')
            in_flight=self.store.db.execute("SELECT COUNT(*) FROM model_calls WHERE state='dispatched'").fetchone()[0]
            if in_flight>=2: raise BrokerWait('Global model concurrency limit reached')
            next_time=self.store.db.execute("SELECT value FROM broker_meta WHERE key='nextDispatchAt'").fetchone()
            if next_time and float(next_time[0])>time.time(): raise BrokerWait('Global model rate limit is active')
            worst=self.book.reservation(payload['max_tokens'],payload)
            call_id='model-'+uuid.uuid4().hex;scopes=[job_id]+(['pilot:first'] if batch['pilot'] else [])
            if job.get('budgetScope'):scopes.append(job['budgetScope'])
            self.store.budgets.reserve(call_id,scopes,worst,fingerprint)
            self.store.budgets.mark_dispatched(call_id)
            call={'id':call_id,'job':job_id,'batch':phase['batch'],'stage':phase['stage'],
                  'generation':phase['generation'],'fingerprint':fingerprint,'payloadSha256':object_hash(payload),
                  'attempt':1 if previous is None else previous['attempt']+1,'state':'dispatched',
                  'startedAt':time.time(),'reserved':worst,'tariff':self.book.data,'tariffHash':self.book.hash,
                  'policy':policy,'response':None,'retryable':False,'inputTokenCeiling':self.book.input_bound(payload)}
            # Private input evidence is necessary to prove which source/pages an
            # independent API review actually received, not merely what it said
            # it reviewed. Never expose this directory as a worker mount.
            payload_path=self.private/(call_id+'.request.json');atomic(payload_path,payload)
            call['request']={'path':str(payload_path),'sha256':digest(payload_path)}
            self.store.db.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?)',
                (call_id,job_id,fingerprint,call['attempt'],call['state'],canonical(call)))
            self.store.db.execute("INSERT OR REPLACE INTO broker_meta VALUES('nextDispatchAt',?)",(str(time.time()+1),))
            self.store.event('model.dispatched',phase['batch'],job_id,request=call_id,stage=phase['stage'],
                             attempt=call['attempt'],reserved=worst,accounting=self.book.data['accounting'])
        return {'action':'dispatch','call':call_id,'payload':payload}

    def finish(self, call_id, response):
        """Settle even rejected/truncated responses; never refund observed usage."""
        call=self.get(call_id)
        if call['state']!='dispatched': raise ValueError('Model call already reconciled')
        try: cost=PriceBook(call['tariff']).settlement(response.get('usage'))
        except (ValueError,AttributeError,KeyError,TypeError):
            return self.unknown(call_id,'usage_unavailable',retryable=False)
        choices=response.get('choices')
        choice=choices[0] if isinstance(choices,list) and len(choices)==1 and isinstance(choices[0],dict) else {}
        message=choice.get('message')
        if not isinstance(message,dict): message={}
        content=message.get('content')
        valid=(isinstance(response.get('id'),str) and bool(response['id']) and
               response.get('model') in (call['policy']['model'],call['tariff'].get('documentedVersion')) and
               choice.get('finish_reason')=='stop' and isinstance(content,str) and bool(content.strip()) and
               not message.get('tool_calls'))
        bound_violation=response['usage']['prompt_tokens']>call.get('inputTokenCeiling',call['tariff']['contextTokenCeiling'])
        if bound_violation:valid=False
        if valid and call['policy'].get('jsonOutput'):
            try:
                if call.get('stage')=='review':
                    from review_json import load_review
                    valid=isinstance(load_review(content),dict)
                else:valid=isinstance(json.loads(content),dict)
            except ValueError: valid=False
        clean={'id':response.get('id'),'model':response.get('model'),'created':response.get('created'),
               'system_fingerprint':response.get('system_fingerprint'),'usage':response['usage'],
               'choices':[{'index':0,'finish_reason':choice.get('finish_reason'),
                           'message':{'role':'assistant','content':content}}]}
        path=self.private/(call_id+'.response.json');atomic(path,clean)
        with self.store.transaction():
            # A pause, cancel, stage change or restart cannot discard an already
            # dispatched provider charge. Delivery is checked separately.
            self.store.budgets.settle(call_id,cost)
            if bound_violation:
                call['errorKind']='input_bound_exceeded'
                self.store.db.execute("INSERT OR REPLACE INTO broker_meta VALUES('fault',?)",('input_bound_exceeded',))
                for raw, in self.store.db.execute('SELECT body FROM batches').fetchall():
                    affected=json.loads(raw)
                    if affected['state']=='running':
                        affected['state']='paused';affected['pauseReason']={'kind':'provider','reason':'input_bound_exceeded'}
                        self.store.save_batch(affected)
                        self.store.event('batch.provider_paused',affected['id'],reason='input_bound_exceeded')
            call.update(state='completed' if valid else 'rejected',finishedAt=time.time(),
                        settledUpperBound=cost,usage=response['usage'],response={'path':str(path),'sha256':digest(path)})
            self.save(call)
            self.store.event('model.'+call['state'],call['batch'],call['job'],request=call_id,
                             settledUpperBound=cost,accounting=call['tariff']['accounting'])
            status=self.store.budgets.status(call['job'])
            if status['spent']>=status['target']:
                notified=self.store.db.execute('SELECT value FROM broker_meta WHERE key=?',('target:'+call['job'],)).fetchone()
                if not notified:
                    self.store.db.execute('INSERT INTO broker_meta VALUES(?,?)',('target:'+call['job'],'1'))
                    self.store.event('budget.target_reached',call['batch'],call['job'],spentUpperBound=status['spent'])
        return {'state':call['state'],'response':clean if valid else None,'call':call_id}

    def unknown(self, call_id, reason, *, retryable=True, retry_after=0):
        with self.store.transaction():
            call=self.get(call_id)
            if call['state']!='dispatched': raise ValueError('Model call already reconciled')
            self.store.budgets.mark_unknown(call_id)
            delay=max(min(float(retry_after),3600),2**call['attempt'])
            call.update(state='unknown',finishedAt=time.time(),errorKind=reason,retryable=bool(retryable),
                        retryAt=time.time()+delay)
            self.save(call)
            self.store.event('model.unknown',call['batch'],call['job'],request=call_id,reason=reason,
                             reserved=call['reserved'],retryable=call['retryable'],retryAt=call['retryAt'])
            if reason in ('authentication','provider_unavailable'):
                self.store.db.execute("INSERT OR REPLACE INTO broker_meta VALUES('fault',?)",(reason,))
                for raw, in self.store.db.execute('SELECT body FROM batches').fetchall():
                    batch=json.loads(raw)
                    if batch['state']!='running':continue
                    batch['state']='paused';batch['pauseReason']={'kind':'provider','reason':reason}
                    self.store.save_batch(batch)
                    self.store.event('batch.provider_paused',batch['id'],reason=reason)
        return {'state':'unknown','call':call_id,'response':None,'retryable':bool(retryable)}

    def recover_controller_restart(self):
        """Call once after OS controller ownership is acquired, never on timeout."""
        ids=[row[0] for row in self.store.db.execute("SELECT id FROM model_calls WHERE state='dispatched'")]
        for call_id in ids:self.unknown(call_id,'controller_restart',retryable=True)
        return ids

    def record_delivery(self,scope,request,call_id):
        call=self.get(call_id)
        if call['state']!='completed' or call['job']!=scope.job:raise ValueError('Invalid private delivery receipt')
        phase=self.store.phase(scope.job)
        with self.store.transaction():
            old=self.store.db.execute('SELECT call_id,request_hash FROM model_deliveries WHERE job_id=? AND generation=? AND request_id=?',
                (scope.job,scope.generation,request['id'])).fetchone()
            if old and tuple(old)!=(call_id,request['sha256']):raise ValueError('Delivery identity changed')
            self.store.db.execute('INSERT OR IGNORE INTO model_deliveries VALUES(?,?,?,?,?,?)',
                (scope.job,phase['stage'],scope.generation,request['id'],call_id,request['sha256']))
