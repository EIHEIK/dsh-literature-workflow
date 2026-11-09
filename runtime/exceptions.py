"""One-use, job-scoped exceptions. No shell, budget or policy escalation."""
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlparse
import uuid

from core import canonical, object_hash, digest


def https_target(url, resolver=socket.getaddrinfo):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment or
            not parsed.hostname or parsed.port not in (None,443) or len(url)>8192):
        raise ValueError('Only bounded public HTTPS targets on port 443 are supported')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    if host.endswith('.') or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
        raise ValueError('Invalid public hostname')
    addresses = sorted({result[4][0] for result in resolver(host, 443, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('Non-public or mixed public/private targets are forbidden')
    return {'host':host, 'port':443, 'addresses':addresses}


class ExceptionStore:
    KINDS = ('https_domain', 'corpus_read', 'process_recovery')

    def __init__(self, store, corpus_roots=()):
        self.store = store
        self.roots = tuple(Path(root).resolve(strict=True) for root in corpus_roots)
        if store.db.in_transaction: raise ValueError('Initialize exception store outside a transaction')
        store.db.execute('CREATE TABLE IF NOT EXISTS bounded_exceptions(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
        store.db.commit()

    def get(self, request_id):
        row = self.store.db.execute('SELECT body FROM bounded_exceptions WHERE id=?', (request_id,)).fetchone()
        if row is None: raise ValueError('Unknown exception request')
        return json.loads(row[0])

    def validate_resource(self, kind, resource):
        if kind == 'https_domain':
            if set(resource) != {'host'}: raise ValueError('Exact public hostname required')
            host = resource['host']
            if not isinstance(host,str) or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,63}', host):
                raise ValueError('No wildcard, IP literal or arbitrary URL grants')
        elif kind == 'corpus_read':
            if set(resource) != {'path','sha256'} or not re.fullmatch('[a-f0-9]{64}',resource['sha256']):
                raise ValueError('Exact corpus file and hash required')
            path = Path(resource['path']).resolve(strict=True)
            if not path.is_file() or not any(path.is_relative_to(root) for root in self.roots):
                raise ValueError('Corpus read outside already authorized roots')
        elif kind == 'process_recovery':
            if set(resource) != {'generation','stage','worker'} or type(resource['generation']) is not int:
                raise ValueError('Exact worker generation required')
            if resource['stage'] not in ('background','translate','compose','review','repair','accept','publish','archive'):
                raise ValueError('Unsupported recovery stage')
            if not isinstance(resource['worker'],str) or not resource['worker']: raise ValueError('Worker identity required')
        else: raise ValueError('Exception category is not eligible for Dot approval')

    def request(self, job_id, kind, resource, reason, *, ttl=3600):
        """Controller only; this method is never exposed as a model tool."""
        self.validate_resource(kind, resource)
        if type(ttl) is not int or not 1<=ttl<=3600: raise ValueError('Exception lifetime must not exceed one hour')
        if not isinstance(reason,str) or not 1<=len(reason)<=1500: raise ValueError('Bounded reason required')
        job = self.store.get(job_id); phase = self.store.phase(job_id)
        rid = 'exception-'+uuid.uuid4().hex
        body = {'id':rid,'job':job_id,'batch':phase['batch'],'kind':kind,'resource':resource,
                'resourceHash':object_hash(resource),'reason':reason,'createdAt':time.time(),
                'expiresAt':time.time()+ttl,'state':'pending','sourceSha256':job['sourceSha256'],
                'configHash':object_hash(job['config']),'review':None}
        with self.store.transaction():
            self.store.db.execute('INSERT INTO bounded_exceptions VALUES(?,?)',(rid,canonical(body)))
            self.store.event('exception.requested',phase['batch'],job_id,request=rid,kindOfException=kind,reason=reason)
        return body

    def review(self, request_id, decision, reason, actor):
        if decision not in ('allow','deny'): raise ValueError('Only allow/deny decisions are supported')
        if not isinstance(reason,str) or not 1<=len(reason)<=1500 or not actor:
            raise ValueError('Review rationale and trusted adapter identity required')
        with self.store.transaction():
            body = self.get(request_id)
            if body['state']!='pending' or body['expiresAt']<time.time(): raise ValueError('Exception already decided or expired')
            self.validate_resource(body['kind'],body['resource'])
            body.update(state='allowed' if decision=='allow' else 'denied',
                        review={'actor':actor,'reason':reason,'at':time.time()})
            self.store.db.execute('UPDATE bounded_exceptions SET body=? WHERE id=?',(canonical(body),request_id))
            self.store.event('exception.'+decision,body['batch'],body['job'],request=request_id)
        return body

    def consume(self, request_id, job_id, resource, revalidate):
        """Controller rechecks DNS/redirects, file hash or actual process + budget.

        The callback is deployment code, not caller-supplied code or a tool arg.
        This permission is only an additional constraint; it never replaces the
        existing worker, path, budget or acceptance gate.
        """
        with self.store.transaction():
            body = self.get(request_id); job = self.store.get(job_id)
            if (body['state']!='allowed' or body['job']!=job_id or body['expiresAt']<time.time() or
                    body['resourceHash']!=object_hash(resource) or body['configHash']!=object_hash(job['config']) or
                    body['sourceSha256']!=job['sourceSha256']): raise ValueError('Exception scope, state or version mismatch')
            self.validate_resource(body['kind'],resource)
            if self.store.batch(body['batch'])['state'] != 'running':
                raise ValueError('Exception cannot execute while its batch is not running')
            if body['kind']=='corpus_read' and digest(resource['path'])!=resource['sha256']:
                raise ValueError('Corpus content changed after exception review')
            if body['kind']=='process_recovery':
                phase = self.store.phase(job_id)
                if (phase['generation']!=resource['generation'] or phase['worker']!=resource['worker'] or
                        phase['stage']!=resource['stage'] or phase['recoveries'].get(phase['stage'],0)>=2):
                    raise ValueError('Recovery identity or retry limit changed')
            if revalidate(body) is not True: raise ValueError('Execution-time policy revalidation failed')
            body.update(state='consumed',consumedAt=time.time())
            self.store.db.execute('UPDATE bounded_exceptions SET body=? WHERE id=?',(canonical(body),request_id))
            self.store.event('exception.consumed',body['batch'],job_id,request=request_id)
        return body
