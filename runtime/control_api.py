"""Narrow control surface shared by CLI, DSH and Dot adapters.

Transport authentication identifies the adapter; tool arguments never select a
role, model, backend, path, budget, command or acceptance policy.
"""
import re

from core import digest,code_hash


def fields(arguments, required=(), optional=()):
    if not isinstance(arguments, dict) or set(arguments)-set(required)-set(optional):
        raise ValueError('Unexpected control arguments')
    if set(required)-set(arguments): raise ValueError('Missing control arguments')


def identifier(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix)+r'[a-f0-9]{16,32}', value):
        raise ValueError('Invalid workflow identifier')
    return value


def zotero_scope(scope):
    fields(scope, ('kind','keys'))
    if scope['kind'] not in ('items','collection'):raise ValueError('Unsupported Zotero scope')
    keys=scope['keys']
    if not isinstance(keys,list) or not 1<=len(keys)<=100 or (scope['kind']=='collection' and len(keys)!=1):
        raise ValueError('Invalid Zotero selection size')
    if any(not isinstance(k,str) or not re.fullmatch('[A-Z0-9]{8}',k) for k in keys):raise ValueError('Invalid Zotero key')
    return scope


class ControlAPI:
    METHODS = ('get_capabilities', 'prepare_batch', 'start_batch', 'get_status', 'read_events',
               'pause_batch', 'resume_batch', 'cancel_batch', 'get_delivery', 'refresh_index', 'get_index_status',
               'get_exceptions','review_exception','export_issue_package')

    def __init__(self, resolver, config, *, pilot=True, readiness=None, index_refresh=None, pilot_readiness=None,batch_authorizer=None,confirmation_method='trusted-host-elicitation',scoped_readiness=None):
        self.resolver = resolver
        self.config = config
        self.pilot = pilot
        self.readiness = readiness
        self.index_refresh = index_refresh
        self.pilot_readiness = pilot_readiness
        self.batch_authorizer = batch_authorizer
        self.confirmation_method = confirmation_method
        self.scoped_readiness=scoped_readiness

    def can_start(self,store,batch):
        if self.batch_authorizer is not None:return self.batch_authorizer(store,batch) is True
        return self.readiness is not None and self.readiness() is True

    def call(self, store, method, arguments, *, actor=None):
        if method not in self.METHODS: raise ValueError('Unsupported control operation')
        if method == 'get_capabilities':
            fields(arguments)
            return {'productionReady':self.readiness is not None and self.readiness() is True,
                    'pilotReady':self.pilot_readiness is not None and self.pilot_readiness() is True,
                    'scopedBatchReady':self.scoped_readiness is not None and self.scoped_readiness() is True,
                    'operations':list(self.METHODS), 'confirmation':self.confirmation_method,
                    'indexRefreshAvailable':self.index_refresh is not None and self.index_refresh.error is None,
                    'remoteGateway':False, 'qwenMTEnabled':False}
        if method in ('refresh_index','get_index_status'):
            if self.index_refresh is None:raise ValueError('Index manager is not installed')
            if method=='get_index_status':
                fields(arguments,('index',))
                return self.index_refresh.get(store,identifier(arguments['index'],'index-'))
            fields(arguments,('scope','requestId'))
            if not isinstance(arguments['requestId'],str) or not re.fullmatch('[a-zA-Z0-9_-]{16,64}',arguments['requestId']):
                raise ValueError('Stable index request ID required')
            if self.index_refresh.error:raise ValueError('Index manager is unavailable')
            return self.index_refresh.request(store,zotero_scope(arguments['scope']),arguments['requestId'])
        if method == 'prepare_batch':
            fields(arguments, ('scope',), ('pages', 'title'))
            scope = zotero_scope(arguments['scope'])
            title = arguments.get('title', '文献翻译批次')
            pages = arguments.get('pages', 'all')
            if not isinstance(title, str) or not 1 <= len(title) <= 200: raise ValueError('Invalid batch title')
            if not isinstance(pages, str) or len(pages)>100: raise ValueError('Invalid source page range')
            inputs, omissions = self.resolver(scope, pages)
            result = store.prepare(inputs, {**self.config,'runtimeRevision':code_hash()}, pilot=self.pilot, title=title)
            return {**result, 'omissions': omissions}
        if method == 'read_events':
            fields(arguments, (), ('after', 'batch', 'limit'))
            batch = arguments.get('batch')
            if batch is not None: identifier(batch, 'batch-')
            return store.read_events(arguments.get('after', 0), batch, arguments.get('limit', 100))
        if method=='review_exception':
            fields(arguments,('request','decision','reason'))
            if not isinstance(actor,str) or not actor.startswith(('codex:','dot:')):
                raise PermissionError('A verified supervisor must review exceptions')
            from exceptions import ExceptionStore
            return ExceptionStore(store,[store.root]).review(identifier(arguments['request'],'exception-'),arguments['decision'],arguments['reason'],actor)
        fields(arguments, ('batch', 'summaryHash') if method=='start_batch' else ('batch',))
        batch = identifier(arguments['batch'], 'batch-')
        if method=='get_exceptions':
            import json
            exists=store.db.execute("SELECT 1 FROM sqlite_master WHERE name='bounded_exceptions'").fetchone()
            store.batch(batch)
            return {'batch':batch,'exceptions':[json.loads(row[0]) for row in store.db.execute('SELECT body FROM bounded_exceptions') if json.loads(row[0])['batch']==batch] if exists else []}
        if method=='export_issue_package':
            from issue_packages import create_package
            packages=[]
            for item in store.batch(batch)['items']:
                phase=store.phase(item['job'])
                if phase['state']=='blocked':
                    if not phase.get('problemPackage'):create_package(store,phase)
                    packages.append({'job':item['job'],'package':store.phase(item['job']).get('problemPackage')})
            return {'batch':batch,'packages':packages,'uploaded':False}
        if method == 'get_status': return {**store.status(batch),'startAllowed':self.can_start(store,batch)}
        if method == 'start_batch':
            if not self.can_start(store,batch):
                raise ValueError('Deployment has not passed production readiness checks')
            return store.start(batch, arguments['summaryHash'])
        if method in ('pause_batch', 'resume_batch', 'cancel_batch'):
            if method=='resume_batch' and not self.can_start(store,batch):
                raise ValueError('Deployment has not passed production readiness checks')
            return store.control(batch, method.removesuffix('_batch'))
        deliveries = []
        for item in store.batch(batch)['items']:
            job = store.get(item['job'])
            if not job.get('publication'): continue
            if not job.get('acceptance') or job['acceptance'] != store.acceptance(job):
                raise ValueError('Delivery no longer matches accepted evidence')
            for artifact in job['publication']['outputs'].values():
                if digest(artifact['path']) != artifact['sha256']: raise ValueError('Published output changed')
            deliveries.append({'job': job['id'], 'title': job['title'], 'sourcePages': job['sourcePages'],
                               'outputs': job['publication']['outputs'], 'archive': job['archive']})
        return {'batch': batch, 'deliveries': deliveries}
