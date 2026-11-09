"""Source-grounded corpus and frozen background packs (no embedding API).

All methods run on the controller's database connection. PDF extraction and
network retrieval happen outside this module, in their restricted adapters.
Extracted text is reference data, never instructions or workflow authority.
"""
import json
import re
import time
from urllib.parse import urlparse

from core import canonical, object_hash, read_json


def bounded_text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Invalid ' + label)
    return value.strip()


class CorpusIndex:
    def __init__(self, db, *, initialize=True):
        self.db = db
        if not initialize:return
        if db.in_transaction: raise ValueError('Initialize corpus outside a transaction')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS corpus_sources(id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS corpus_passages(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,body TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(id UNINDEXED,text,tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS corpus_translations(id TEXT PRIMARY KEY,kind TEXT NOT NULL,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS corpus_trust(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,acceptance_hash TEXT NOT NULL);
        ''')

    def add_source(self, reference, passages):
        """Trusted retrieval adapter passes provenance and the verified bytes hash."""
        if not isinstance(reference, dict) or reference.get('kind') not in ('paper', 'zotero', 'web'):
            raise ValueError('Explicit corpus source provenance required')
        if not re.fullmatch('[a-f0-9]{64}', reference.get('sha256', '')):
            raise ValueError('Source content hash required')
        bounded_text(reference.get('title'), 1000, 'source title')
        if reference['kind'] == 'web':
            url = urlparse(reference.get('url', ''))
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('Public source requires a credential-free HTTPS URL')
            # Public address and redirect checks belong to the retrieval proxy;
            # a URL's spelling alone is never evidence of network authorization.
            if not reference.get('retrievalReceipt'):
                raise ValueError('Verified retrieval receipt required')
        elif not re.fullmatch('[A-Z0-9]{8}', reference.get('attachmentKey', '')):
            if reference['kind'] != 'paper' or not reference.get('job'):
                raise ValueError('Local source attachment or paper identity required')
        if type(reference.get('retrievedAt')) not in (int, float) or reference['retrievedAt'] <= 0:
            raise ValueError('Source retrieval timestamp required')
        if not isinstance(passages, list) or not 1 <= len(passages) <= 10000:
            raise ValueError('Invalid extracted passage count')
        source_id = object_hash(reference)
        self.db.execute('INSERT OR IGNORE INTO corpus_sources VALUES(?,?)', (source_id, canonical(reference)))
        ids = []
        for passage in passages:
            if type(passage.get('page')) is not int or passage['page'] < 1:
                raise ValueError('Source physical page required')
            bounded_text(passage.get('anchor'), 200, 'passage anchor')
            text = bounded_text(passage.get('text'), 30000, 'passage text')
            body = {**passage, 'source': source_id, 'text': text, 'trust': 'reference_only'}
            pid = object_hash(body)
            inserted = self.db.execute('INSERT OR IGNORE INTO corpus_passages VALUES(?,?,?)',
                                       (pid, source_id, canonical(body))).rowcount
            if inserted: self.db.execute('INSERT INTO corpus_fts(id,text) VALUES(?,?)', (pid, text))
            ids.append(pid)
        return {'source': source_id, 'passages': ids}

    def passage(self, passage_id):
        row = self.db.execute('SELECT body FROM corpus_passages WHERE id=?', (passage_id,)).fetchone()
        if row is None: raise ValueError('Unknown corpus passage')
        body = json.loads(row[0])
        if object_hash(body) != passage_id: raise ValueError('Corpus passage changed')
        source = self.db.execute('SELECT body FROM corpus_sources WHERE id=?', (body['source'],)).fetchone()
        if source is None: raise ValueError('Corpus source missing')
        reference = json.loads(source[0])
        if object_hash(reference) != body['source']: raise ValueError('Corpus provenance changed')
        return {'id': passage_id, **body, 'reference': reference}

    def search(self, query, *, limit=30):
        bounded_text(query, 1000, 'corpus query')
        if type(limit) is not int or not 1 <= limit <= 100: raise ValueError('Invalid search limit')
        # Do not interpret model-provided FTS operators or SQL. Query terms are
        # quoted individually; scientific abbreviations remain searchable.
        terms = list(dict.fromkeys(re.findall(r'\w+', query, flags=re.UNICODE)))[:20]
        stop={'a','an','the','of','for','in','on','to','and','or','with','by','from','using','based','study','paper','method'}
        terms=[term for term in terms if term.lower() not in stop]
        if not terms: return []
        expression = ' OR '.join('"'+term+'"' for term in terms)
        rows = self.db.execute('SELECT id FROM corpus_fts WHERE corpus_fts MATCH ? ORDER BY rank LIMIT ?',
                               (expression, limit)).fetchall()
        return [self.passage(row[0]) for row in rows]

    def review_candidates(self,job):
        """Read exact candidate identities without DDL inside an acceptance transaction."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='corpus_translations'").fetchone():return []
        selected=[];counts={}
        for tid,raw in self.db.execute('SELECT id,body FROM corpus_translations ORDER BY kind,id'):
            body=json.loads(raw)
            if body['producerJob']!=job['id']:continue
            if object_hash(body)!=tid:raise ValueError('Candidate translation changed')
            passage=self.passage(body['passage']);ref=passage['reference'];page=passage['page']
            if ref['kind']!='paper' or ref.get('job')!=job['id'] or ref['sha256']!=job['sourceSha256']:
                raise ValueError('Corpus candidate is not grounded in its producing paper')
            if body['source'] not in passage['text']:raise ValueError('Corpus source segment changed')
            group=(page,body['kind']);cap=5 if body['kind']=='memory' else 25
            if page not in job['sourcePages'] or counts.get(group,0)>=cap:continue
            counts[group]=counts.get(group,0)+1
            selected.append({'id':tid,'kind':body['kind'],'source':body['source'],'target':body['target'],
                             'domain':body['domain'],'sourcePage':page,'anchor':passage['anchor'],'passage':body['passage']})
        return selected

    def add_translation(self, kind, source, target, domain, passage_id, *, producer_job):
        if kind not in ('term', 'memory'): raise ValueError('Invalid corpus translation kind')
        passage = self.passage(passage_id)
        source = bounded_text(source, 10000, 'source segment')
        if source not in passage['text']: raise ValueError('Translation source not found at cited passage')
        body = {'kind': kind, 'source': source, 'target': bounded_text(target, 10000, 'translation'),
                'domain': bounded_text(domain, 200, 'term domain'), 'passage': passage_id,
                'producerJob': producer_job, 'status': 'candidate'}
        translation_id = object_hash(body)
        self.db.execute('INSERT OR IGNORE INTO corpus_translations VALUES(?,?,?)', (translation_id, kind, canonical(body)))
        return translation_id

    def promote(self, store, job_id, translation_ids):
        """Only IDs explicitly checked in the accepted independent report qualify.

        A passing PDF report without corpusReview IDs cannot bless all extracted
        terms. The controller's API review protocol must bind these exact IDs.
        """
        job = store.get(job_id)
        if not job.get('acceptance') or job['acceptance'] != store.acceptance(job):
            raise ValueError('Current accepted review required for corpus promotion')
        report = read_json(job['acceptance']['reviewFile']['path'])
        reviewed = report.get('corpusReview', {})
        verified = set(reviewed.get('verifiedTermIds', [])) | set(reviewed.get('verifiedMemoryIds', []))
        for translation_id in translation_ids:
            row = self.db.execute('SELECT body FROM corpus_translations WHERE id=?', (translation_id,)).fetchone()
            if row is None or translation_id not in verified: raise ValueError('Translation not independently verified')
            body = json.loads(row[0])
            if object_hash(body) != translation_id or body['producerJob'] != job_id:
                raise ValueError('Translation provenance or producer mismatch')
            expected = 'verifiedTermIds' if body['kind']=='term' else 'verifiedMemoryIds'
            if translation_id not in reviewed.get(expected, []): raise ValueError('Corpus review kind mismatch')
            self.passage(body['passage'])
            self.db.execute('INSERT OR REPLACE INTO corpus_trust VALUES(?,?,?)',
                            (translation_id, job_id, object_hash(job['acceptance'])))

    def trusted_translations(self, store, domain):
        result = []
        for tid, raw, job_id, acceptance_hash in self.db.execute('''
                SELECT t.id,t.body,v.job_id,v.acceptance_hash FROM corpus_translations t
                JOIN corpus_trust v ON v.id=t.id'''):
            body = json.loads(raw)
            if body['domain'] != domain: continue
            if object_hash(body) != tid: raise ValueError('Trusted translation changed')
            job = store.get(job_id)
            try:
                if not job.get('acceptance') or object_hash(store.acceptance(job)) != acceptance_hash: continue
                self.passage(body['passage'])
            except (ValueError, OSError): continue
            result.append({'id': tid, **body, 'status': 'trusted', 'acceptanceHash': acceptance_hash})
        return result

    def freeze_pack(self, store, job_id, own_passages, query, domain):
        job = store.get(job_id)
        own = [self.passage(pid) for pid in own_passages]
        if not own: raise ValueError('Paper background requires its own grounded passages')
        for passage in own:
            ref = passage['reference']
            if ref['kind'] != 'paper' or ref.get('job') != job_id or ref['sha256'] != job['sourceSha256']:
                raise ValueError('Paper background source mismatch')
        own_sources = {p['source'] for p in own}
        local, web, selected, chars = set(), set(), [], 0
        for passage in self.search(query, limit=100):
            if passage['source'] in own_sources or passage['reference']['sha256']==job['sourceSha256']: continue
            kind = passage['reference']['kind']
            if kind == 'paper': continue
            pool, cap = (web, 5) if kind=='web' else (local, 10)
            fingerprint=passage['reference']['sha256']
            if fingerprint not in pool and len(pool)>=cap: continue
            if chars+len(passage['text']) > 30000: continue
            pool.add(fingerprint); selected.append(passage); chars += len(passage['text'])
        pack = {'schema': 1, 'job': job_id, 'sourceSha256': job['sourceSha256'],
                'domain': domain, 'own': own, 'references': selected,
                'translations': self.trusted_translations(store, domain),
                'policy': {'ownDefinitionsFirst': True, 'referencesAreInstructions': False,
                           'noUnsupportedAdditions': True, 'maxLocalSources': 10, 'maxWebSources': 5}}
        return {'id': object_hash(pack), **pack}
